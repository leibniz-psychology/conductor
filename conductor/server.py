import asyncio, socket, os, struct, stat, json, logging, argparse, functools, traceback, time
from http.cookies import BaseCookie
from collections import namedtuple

import aionotify
from yarl import URL
from multidict import CIMultiDict
from parse import parse

from .nss import getUser
from .util import proxy

logger = logging.getLogger (__name__)

def handleException (func):
	""" XXX: Should probably use some http library here? """
	@functools.wraps(func)
	async def wrapped(*args):
		 # Some fancy foo stuff
		try:
			return await func(*args)
		except Exception as e:
			self, reader, writer = args
			traceback.print_exc()
			logger.error (f'exception {e.args}')
			writer.write (b'HTTP/1.0 500 Server Error\r\n\r\n')
			writer.close ()
	return wrapped

# a route from key.domain to socket with auth
Route = namedtuple ('Route', ['socket', 'key', 'auth'])
RouteKey = namedtuple ('RouteKey', ['key', 'user'])

class Conductor:
	__slots__ = ('routes', 'domain', 'nextConnId', 'status')

	def __init__ (self, domain):
		self.domain = domain
		self.routes = {}
		self.nextConnId = 0
		self.status = dict (
			requestTotal=0,
			requestActive=0,
			unauthorized=0,
			noroute=0,
			broken=0,
			)

	async def _connectCb (self, reader, writer):
		self.status['requestTotal'] += 1
		self.status['requestActive'] += 1
		try:
			await self.handleRequest (reader, writer)
		finally:
			self.status['requestActive'] -= 1

	@handleException
	async def handleRequest (self, reader, writer):
		# connection id, for debugging
		connid = self.nextConnId
		self.nextConnId += 1

		logger.debug (f'{connid}: new incoming connection')
		# simple HTTP parsing
		l = (await reader.readline ()).rstrip (b'\r\n')
		try:
			method, path, proto = l.split (b' ')
		except ValueError:
			logger.error (f'{connid}: cannot split line {l}')
			raise
		path = path.split (b'/')

		headers = CIMultiDict ()
		while True:
			if len (headers) > 100:
				raise Exception ('too many headers')

			l = (await reader.readline ()).rstrip (b'\r\n')
			# end of headers?
			if l == b'\r\n' or not l:
				break
			try:
				key, value = l.decode ('utf-8').split (':', 1)
				headers.add (key.strip (), value.strip ())
			except ValueError:
				logger.error (f'cannot parse {l}')

		logger.debug (f'{connid}: {path} {method} got headers {headers}')

		route = None
		try:
			host = headers['host']
			routeKey = None
			for d in self.domain:
				m = parse (d, host)
				if m is not None:
					routeKey = RouteKey (key=m['key'], user=m['user'])
					logger.debug (f'{connid}: got route key {routeKey}')
					break
			route = self.routes[routeKey]
		except (KeyError, ValueError):
			logger.info (f'{connid}: cannot find route for host {host} match {m} domain {self.domain}')
			self.status['noroute'] += 1
			# error is written to client later

		# is this a non-forwarded request?
		if path[1] == b'_conductor':
			if path[2] == b'auth':
				logger.info (f'authorization request for {host}')
				writer.write (b'\r\n'.join ([
						b'HTTP/1.0 302 Found',
						b'Location: /',
						b'Set-Cookie: authorization=' + path[3] + b'; HttpOnly; Path=/',
						b'Cache-Control: no-store',
						b'',
						b'Follow the white rabbit.']))
			elif path[2] == b'status':
				writer.write (b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n')
				self.status['routesTotal'] = len (self.routes)
				writer.write (json.dumps (self.status, ensure_ascii=True).encode ('ascii'))
			else:
				writer.write (b'HTTP/1.0 404 Not Found\r\nContent-Type: plain/text\r\n\r\nNot found')
			writer.close ()
			return

		if not route:
			writer.write (b'HTTP/1.0 404 Not Found\r\n\r\n')
			writer.close ()
			return

		# check authorization
		cookies = BaseCookie()
		try:
			cookies.load (headers['Cookie'])
		except KeyError:
			# will be rejected later
			pass
		authorized = False
		for c in cookies.values ():
			if c.key == 'authorization' and c.value == route.auth:
				authorized = True
		try:
			del cookies['authorization']
			headers['Cookie'] = cookies.output (header='', sep='')
		except KeyError:
			# nonexistent cookie is fine
			pass

		if not authorized:
			logger.info (f'{connid}: not authorized, cookies sent {cookies.values()}')
			writer.write (b'HTTP/1.0 403 Unauthorized\r\nContent-Type: plain/text\r\n\r\nUnauthorized')
			writer.close ()
			self.status['unauthorized'] += 1
			return

		# try opening the socket
		try:
			start = time.time ()
			sockreader, sockwriter = await asyncio.open_unix_connection (path=route.socket)
			end = time.time ()
			logger.debug (f'opening socket took {end-start}s')
		except (ConnectionRefusedError, FileNotFoundError, PermissionError):
			logger.info (f'{connid}: route {routeKey} is broken')
			writer.write (b'HTTP/1.0 502 Bad Gateway\r\n\r\n')
			writer.close ()
			self.status['broken'] += 1
			return

		# some headers are fixed
		# not parsing body, so we cannot handle more than one request per connection
		# XXX: this is super-inefficient
		if 'Upgrade' not in headers or headers['Upgrade'].lower () != 'websocket':
			headers['Connection'] = 'close'

		# write http banner plus headers
		sockwriter.write (method + b' ' + b'/'.join (path) + b' ' + proto + b'\r\n')
		for k, v in headers.items ():
			sockwriter.write (f'{k}: {v}\r\n'.encode ('utf-8'))
		sockwriter.write (b'\r\n')

		async def beforeABClose (result):
			if result == 0:
				# no response received from client
				logger.info (f'{connid}: route {routeKey} got no result from server')
				writer.write (b'HTTP/1.0 502 Bad Gateway\r\n\r\n')

		await proxy ((sockreader, sockwriter, 'sock'), (reader, writer, 'web'), logger=logger, logPrefix=connid, beforeABClose=beforeABClose)

	async def run (self, port=None, sock=None):
		"""
		Start public proxy worker
		"""

		logger.info (f'starting server on port {port} for domains {self.domain}')
		server = await asyncio.start_server (self._connectCb, port=port, sock=sock)
		await server.serve_forever ()

	def addRoute (self, route):
		assert isinstance (route.key, RouteKey)
		if route.key in self.routes:
			raise KeyError ('exists')
		self.routes[route.key] = route
		logger.info (f'configured route {route.key} → {route.socket}')

	def deleteRoute (self, key):
		route = self.routes.pop (key)
		logger.info (f'removed route {route.key} → {route.socket}')

class Forest:
	__slots__ = ('path', 'proxy', 'pathToRoute')

	def __init__ (self, path, proxy):
		self.path = os.path.realpath (path)
		self.proxy = proxy
		self.pathToRoute = dict ()

		self._forestPermissionCheck ()

	def _forestPermissionCheck (self):
		# make sure the forest dir has appropriate permissions
		fstat = os.stat (self.path, follow_symlinks=False)
		permissions = stat.S_IMODE (fstat.st_mode)
		expect = 0o3713
		if permissions != expect:
			logger.error (f'Permissions on forest directory {self.path} are not correct. Is 0{permissions:o} should be 0{expect:o}')
			raise Exception ()

	async def addConfig (self, f):
		"""
		Add file f relative to forest path to proxy configuration
		"""

		configFile = os.path.join (self.path, f)
		if not configFile.endswith ('.json'):
			# skip files that are not configs
			logger.error (f'{configFile} does not end with .json')
			return

		try:
			cstat = os.stat (configFile, follow_symlinks=False)
			# don’t read if it is world-readable -> leaked credentials
			permissions = stat.S_IMODE (cstat.st_mode)
			if not stat.S_ISREG (cstat.st_mode):
				raise Exception (f'{configFile} not a regular file')
			if permissions & 0o4:
				raise Exception (f'{configFile} world-readable, ignoring')

			with open (configFile) as fd:
				o = json.load (fd)

			# socket checks:
			socketPath = os.path.realpath (os.path.join (self.path, o['socket']))
			if not os.path.dirname (socketPath).startswith (self.path):
				raise Exception (f'{socketPath} not in forest dir {self.path}')
			sstat = os.stat (socketPath, follow_symlinks=False)
			permissions = stat.S_IMODE (sstat.st_mode)
			# neither world-read nor writeable
			if permissions & 0o6:
				raise Exception (f'{socketPath} world-read- or -writeable')
			# same owner
			if cstat.st_uid != sstat.st_uid:
				raise Exception (f'{socketPath} socket owner does not match')
			if not stat.S_ISSOCK (sstat.st_mode):
				raise Exception (f'{socketPath} not a socket')

			user = getUser (cstat.st_uid)
			key = RouteKey (key=str (o.get ('key')), user=user['name'])
			route = Route (socket=socketPath, key=key, auth=o['auth'])

			self.proxy.addRoute (route)
			self.pathToRoute[f] = route

			o.update (dict (urls=[d.format (key=key.key, user=key.user) for d in self.proxy.domain]))
			logger.debug (f'updating config at {configFile}: {o}')
			# /proc/sys/fs/protected_regular > 0 prevents using
			# > with open (configFile, 'w') as fd:
			# because it contains O_CREAT, so we have to do it manually
			with os.fdopen (os.open (configFile, os.O_TRUNC|os.O_WRONLY), 'w') as fd:
				json.dump (o, fd)
				fd.write ('\n')
		except Exception as e:
			logger.error (f'addConfig for {f} failed: {e}')
			try:
				with os.fdopen (os.open (configFile, os.O_TRUNC|os.O_WRONLY), 'w') as fd:
					json.dump (dict (status='error', reason=e.args[0]), fd)
					fd.write ('\n')
			except PermissionError:
				# can’t write error, sorry
				logger.error (f'not allowed to write error to config {configFile} ')

	async def removeConfig (self, f):
		try:
			r = self.pathToRoute.pop (f)
			self.proxy.deleteRoute (r.key)
		except KeyError:
			pass

	async def run (self):
		"""
		Start directory watcher worker
		"""

		# add existing files
		logger.info (f'reading existing configs in {self.path}')
		for f in os.listdir (self.path):
			await self.addConfig (f)

		logger.info ('starting watcher')
		watcher = aionotify.Watcher()
		watcher.watch (path=self.path,
				flags=aionotify.Flags.MOVED_TO|aionotify.Flags.DELETE)

		await watcher.setup(asyncio.get_event_loop ())
		while True:
			event = await watcher.get_event()
			if event.flags == aionotify.Flags.MOVED_TO:
				await self.addConfig (event.name)
			elif event.flags == aionotify.Flags.DELETE:
				await self.removeConfig (event.name)
			else:
				assert False
		watcher.close ()

def main ():
	parser = argparse.ArgumentParser(description='conductor server part')
	parser.add_argument ('-p', '--port', default=8888, type=int,
		metavar='PORT', help='Listen port')
	parser.add_argument ('-s', '--sock', default=None, metavar='PATH',
		help='Listen port')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('domain', nargs='+', metavar='DOMAIN', help='Domain match pattern')
	parser.add_argument ('forest', default='forest', metavar='PATH',
		help='Local forest path')

	args = parser.parse_args()
	if args.verbose:
		logging.basicConfig (level=logging.DEBUG)
	else:
		logging.basicConfig (level=logging.WARN)

	if args.sock:
		sock = socket.socket (socket.AF_UNIX)
		if os.path.exists (args.sock):
			os.unlink (args.sock)
		sock.bind (args.sock)
		os.chmod (args.sock, 0o660)
		port = None
	else:
		sock = None
		port = args.port

	loop = asyncio.get_event_loop ()
	proxy = Conductor (args.domain)
	loop.create_task (proxy.run (port, sock))
	forest = Forest (args.forest, proxy)
	loop.run_until_complete (forest.run ())

