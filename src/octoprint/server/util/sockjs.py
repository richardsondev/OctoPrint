# coding=utf-8
from __future__ import absolute_import, division, print_function

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'
__copyright__ = "Copyright (C) 2014 The OctoPrint Project - Released under terms of the AGPLv3 License"

import logging
import threading
import sockjs.tornado
import sockjs.tornado.session
import sockjs.tornado.proto
import sockjs.tornado.util
import time

import octoprint.timelapse
import octoprint.server
import octoprint.events
import octoprint.plugin

from octoprint.events import Events
from octoprint.settings import settings
from octoprint.util.json import JsonEncoding

import octoprint.printer

import wrapt
import json


def fix_tornado5_compatibility():
	"""
	Monkey patches sockjs tornado dependency to fix tornado 5.x compatibility

	See https://github.com/mrjoes/sockjs-tornado/issues/113 for reason.

	We need to first patch StatsCollector since that also gets used by SockJSRouter.
	"""

	import sockjs.tornado.router
	import sockjs.tornado.stats
	from tornado import ioloop, version_info

	def fixed_stats_init(self, io_loop):
		### Copied from sockjs.tornado.stats.StatsCollector.__init__

		# Sessions
		self.sess_active = 0

		# Avoid circular reference
		self.sess_transports = dict()

		# Connections
		self.conn_active = 0
		self.conn_ps = sockjs.tornado.stats.MovingAverage()

		# Packets
		self.pack_sent_ps = sockjs.tornado.stats.MovingAverage()
		self.pack_recv_ps = sockjs.tornado.stats.MovingAverage()

		self._callback = ioloop.PeriodicCallback(self._update,
		                                         1000)
		self._callback.start()

	sockjs.tornado.stats.StatsCollector.__init__ = fixed_stats_init

	def fixed_router_init(self, connection, prefix="", user_settings=dict(), io_loop=None, session_kls=None):
		### Copied from sockjs.tornado.router.SockJSRouter.__init__

		# TODO: Version check
		if version_info[0] < 2:
			raise Exception('sockjs-tornado requires Tornado 2.0 or higher.')

		# Store connection class
		self._connection = connection

		# Initialize io_loop
		self.io_loop = io_loop or ioloop.IOLoop.instance()

		# Settings
		self.settings = sockjs.tornado.router.DEFAULT_SETTINGS.copy()
		if user_settings:
			self.settings.update(user_settings)

		self.websockets_enabled = 'websocket' not in self.settings['disabled_transports']
		self.cookie_needed = self.settings['jsessionid']

		# Sessions
		self._session_kls = session_kls if session_kls else sockjs.tornado.router.session.Session
		self._sessions = sockjs.tornado.router.sessioncontainer.SessionContainer()

		check_interval = self.settings['session_check_interval'] * 1000
		self._sessions_cleanup = ioloop.PeriodicCallback(self._sessions.expire,
		                                                 check_interval)
		self._sessions_cleanup.start()

		# Stats
		self.stats = sockjs.tornado.stats.StatsCollector(self.io_loop)

		# Initialize URLs
		base = prefix + r'/[^/.]+/(?P<session_id>[^/.]+)'

		# Generate global handler URLs
		self._transport_urls = [('%s/%s$' % (base, p[0]), p[1], dict(server=self))
		                        for p in sockjs.tornado.router.GLOBAL_HANDLERS]

		for k, v in sockjs.tornado.router.TRANSPORTS.items():
			if k in self.settings['disabled_transports']:
				continue

			# Only version 1 is supported
			self._transport_urls.append(
				(r'%s/%s$' % (base, k),
				 v,
				 dict(server=self))
			)

		# Generate static URLs
		self._transport_urls.extend([('%s%s' % (prefix, k), v, dict(server=self))
		                             for k, v in sockjs.tornado.router.STATIC_HANDLERS.items()])

	sockjs.tornado.router.SockJSRouter.__init__ = fixed_router_init


class ThreadSafeSession(sockjs.tornado.session.Session):
	def __init__(self, conn, server, session_id, expiry=None):
		sockjs.tornado.session.Session.__init__(self, conn, server, session_id, expiry=expiry)

	def set_handler(self, handler, start_heartbeat=True):
		if getattr(handler, "__orig_send_pack", None) is None:
			orig_send_pack = handler.send_pack
			mutex = threading.RLock()

			def send_pack(*args, **kwargs):
				with mutex:
					return orig_send_pack(*args, **kwargs)

			handler.send_pack = send_pack
			setattr(handler, "__orig_send_pack", orig_send_pack)

		return sockjs.tornado.session.Session.set_handler(self, handler, start_heartbeat=start_heartbeat)

	def remove_handler(self, handler):
		result = sockjs.tornado.session.Session.remove_handler(self, handler)

		if getattr(handler, "__orig_send_pack", None) is not None:
			handler.send_pack = getattr(handler, "__orig_send_pack")
			delattr(handler, "__orig_send_pack")

		return result


class JsonEncodingSessionWrapper(wrapt.ObjectProxy):
	def send_message(self, msg, stats=True, binary=False):
		self.send_jsonified(json.dumps(sockjs.tornado.util.bytes_to_str(msg),
		                               separators=(',', ':'),
		                               default=JsonEncoding.encode),
		                    stats)


class PrinterStateConnection(sockjs.tornado.SockJSConnection, octoprint.printer.PrinterCallback):
	def __init__(self, printer, fileManager, analysisQueue, userManager, eventManager, pluginManager, session):
		if isinstance(session, sockjs.tornado.session.Session):
			session = JsonEncodingSessionWrapper(session)

		sockjs.tornado.SockJSConnection.__init__(self, session)

		self._logger = logging.getLogger(__name__)

		self._temperatureBacklog = []
		self._temperatureBacklogMutex = threading.Lock()
		self._logBacklog = []
		self._logBacklogMutex = threading.Lock()
		self._messageBacklog = []
		self._messageBacklogMutex = threading.Lock()

		self._printer = printer
		self._fileManager = fileManager
		self._analysisQueue = analysisQueue
		self._userManager = userManager
		self._eventManager = eventManager
		self._pluginManager = pluginManager

		self._remoteAddress = None

		self._throttleFactor = 1
		self._lastCurrent = 0
		self._baseRateLimit = 0.5

	def _getRemoteAddress(self, info):
		forwardedFor = info.headers.get("X-Forwarded-For")
		if forwardedFor is not None:
			return forwardedFor.split(",")[0]
		return info.ip

	def __str__(self):
		if self._remoteAddress:
			return "{!r} connected to {}".format(self, self._remoteAddress)
		else:
			return "Unconnected {!r}".format(self)

	def on_open(self, info):
		self._remoteAddress = self._getRemoteAddress(info)
		self._logger.info("New connection from client: %s" % self._remoteAddress)

		plugin_signature = lambda impl: "{}:{}".format(impl._identifier, impl._plugin_version)
		template_plugins = map(plugin_signature, self._pluginManager.get_implementations(octoprint.plugin.TemplatePlugin))
		asset_plugins = map(plugin_signature, self._pluginManager.get_implementations(octoprint.plugin.AssetPlugin))
		ui_plugins = sorted(set(template_plugins + asset_plugins))

		import hashlib
		plugin_hash = hashlib.md5()
		plugin_hash.update(",".join(ui_plugins))

		config_hash = settings().config_hash

		# connected => update the API key, might be necessary if the client was left open while the server restarted
		self._emit("connected", dict(
			apikey=octoprint.server.UI_API_KEY,
			version=octoprint.server.VERSION,
			display_version=octoprint.server.DISPLAY_VERSION,
			branch=octoprint.server.BRANCH,
			plugin_hash=plugin_hash.hexdigest(),
			config_hash=config_hash,
			debug=octoprint.server.debug,
			safe_mode=octoprint.server.safe_mode
		))

		self._printer.register_callback(self)
		self._fileManager.register_slicingprogress_callback(self)
		octoprint.timelapse.register_callback(self)
		self._pluginManager.register_message_receiver(self.on_plugin_message)

		self._eventManager.fire(Events.CLIENT_OPENED, {"remoteAddress": self._remoteAddress})
		for event in octoprint.events.all_events():
			self._eventManager.subscribe(event, self._onEvent)

		octoprint.timelapse.notify_callbacks(octoprint.timelapse.current)

		# This is a horrible hack for now to allow displaying a notification that a render job is still
		# active in the backend on a fresh connect of a client. This needs to be substituted with a proper
		# job management for timelapse rendering, analysis stuff etc that also gets cancelled when prints
		# start and so on.
		#
		# For now this is the easiest way though to at least inform the user that a timelapse is still ongoing.
		#
		# TODO remove when central job management becomes available and takes care of this for us
		if octoprint.timelapse.current_render_job is not None:
			self._emit("event", {"type": Events.MOVIE_RENDERING, "payload": octoprint.timelapse.current_render_job})

	def on_close(self):
		self._printer.unregister_callback(self)
		self._fileManager.unregister_slicingprogress_callback(self)
		octoprint.timelapse.unregister_callback(self)
		self._pluginManager.unregister_message_receiver(self.on_plugin_message)

		self._eventManager.fire(Events.CLIENT_CLOSED, {"remoteAddress": self._remoteAddress})
		for event in octoprint.events.all_events():
			self._eventManager.unsubscribe(event, self._onEvent)

		self._logger.info("Client connection closed: %s" % self._remoteAddress)
		self._remoteAddress = None

	def on_message(self, message):
		try:
			import json
			message = json.loads(message)
		except:
			self._logger.warn("Invalid JSON received from client {}, ignoring: {!r}".format(self._remoteAddress, message))
			return

		if "throttle" in message:
			try:
				throttle = int(message["throttle"])
				if throttle < 1:
					raise ValueError()
			except ValueError:
				self._logger.warn("Got invalid throttle factor from client {}, ignoring: {!r}".format(self._remoteAddress, message["throttle"]))
			else:
				self._throttleFactor = throttle
				self._logger.debug("Set throttle factor for client {} to {}".format(self._remoteAddress, self._throttleFactor))

	def on_printer_send_current_data(self, data):
		# make sure we rate limit the updates according to our throttle factor
		now = time.time()
		if now < self._lastCurrent + self._baseRateLimit * self._throttleFactor:
			return
		self._lastCurrent = now

		# add current temperature, log and message backlogs to sent data
		with self._temperatureBacklogMutex:
			temperatures = self._temperatureBacklog
			self._temperatureBacklog = []

		with self._logBacklogMutex:
			logs = self._logBacklog
			self._logBacklog = []

		with self._messageBacklogMutex:
			messages = self._messageBacklog
			self._messageBacklog = []

		busy_files = [dict(origin=v[0], path=v[1]) for v in self._fileManager.get_busy_files()]
		if "job" in data and data["job"] is not None \
				and "file" in data["job"] and "path" in data["job"]["file"] and "origin" in data["job"]["file"] \
				and data["job"]["file"]["path"] is not None and data["job"]["file"]["origin"] is not None \
				and (self._printer.is_printing() or self._printer.is_paused()):
			busy_files.append(dict(origin=data["job"]["file"]["origin"], path=data["job"]["file"]["path"]))

		data.update({
			"serverTime": time.time(),
			"temps": temperatures,
			"logs": logs,
			"messages": messages,
			"busyFiles": busy_files,
		})
		self._emit("current", data)

	def on_printer_send_initial_data(self, data):
		data_to_send = dict(data)
		data_to_send["serverTime"] = time.time()
		self._emit("history", data_to_send)

	def sendEvent(self, type, payload=None):
		self._emit("event", {"type": type, "payload": payload})

	def sendTimelapseConfig(self, timelapseConfig):
		self._emit("timelapse", timelapseConfig)

	def sendSlicingProgress(self, slicer, source_location, source_path, dest_location, dest_path, progress):
		self._emit("slicingProgress",
		           dict(slicer=slicer, source_location=source_location, source_path=source_path, dest_location=dest_location, dest_path=dest_path, progress=progress)
		)

	def on_plugin_message(self, plugin, data):
		self._emit("plugin", dict(plugin=plugin, data=data))

	def on_printer_add_log(self, data):
		with self._logBacklogMutex:
			self._logBacklog.append(data)

	def on_printer_add_message(self, data):
		with self._messageBacklogMutex:
			self._messageBacklog.append(data)

	def on_printer_add_temperature(self, data):
		with self._temperatureBacklogMutex:
			self._temperatureBacklog.append(data)

	def _onEvent(self, event, payload):
		self.sendEvent(event, payload)

	def _emit(self, type, payload):
		try:
			self.send({type: payload})
		except Exception as e:
			if self._logger.isEnabledFor(logging.DEBUG):
				self._logger.exception("Could not send message to client {}".format(self._remoteAddress))
			else:
				self._logger.warn("Could not send message to client {}: {}".format(self._remoteAddress, e))
