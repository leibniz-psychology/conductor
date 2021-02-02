# Copyright 2019–2020 Leibniz Institute for Psychology
# 
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
# 
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.

import asyncio, socket, os, struct, stat, json, logging, argparse, functools, \
		traceback, time, tempfile, pwd
from http.cookies import BaseCookie
from collections import namedtuple
from hashlib import blake2b
from furl import furl

from multidict import CIMultiDict
from parse import parse

from .util import proxy

logger = logging.getLogger (__name__)

class BadRequest (Exception):
	pass

def handleException (func):
	""" XXX: Should probably use some http library here? """
	@functools.wraps(func)
	async def wrapped(*args):
		 # Some fancy foo stuff
		try:
			return await func(*args)
		except BadRequest as e:
			self, reader, writer = args
			logger.error (f'Bad request: {e}')
			writer.write (b'HTTP/1.0 400 Bad Request\r\n\r\n')
			writer.close ()
		except Exception as e:
			self, reader, writer = args
			traceback.print_exc()
			logger.error (f'uncaught exception {e.args}')
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
			writer.close ()
			await writer.wait_closed ()
			self.status['requestActive'] -= 1

	@staticmethod
	def hashKey (v):
		return blake2b (v.encode ('utf-8')).hexdigest ()

	@handleException
	async def handleRequest (self, reader, writer):
		# connection id, for debugging
		connid = self.nextConnId
		self.nextConnId += 1

		logger.debug (f'{connid}: new incoming connection')
		# simple HTTP parsing, yes this is a terrible idea.
		l = await reader.readline ()
		if l == bytes ():
			raise BadRequest (f'{connid}: unexpected eof')
		l = l.rstrip (b'\r\n')
		try:
			method, rawPath, proto = l.split (b' ')
			logger.debug (f'{connid}: got {method} {rawPath} {proto}')
		except ValueError:
			logger.error (f'{connid}: cannot split line {l}')
			raise
		reqUrl = furl (rawPath.decode ('utf-8'))

		headers = CIMultiDict ()
		while True:
			if len (headers) > 100:
				raise BadRequest ('too many headers')

			l = await reader.readline ()
			if l == bytes ():
				raise BadRequest (f'{connid}: unexpected eof in headers')
			l = l.rstrip (b'\r\n')
			logger.debug (f'{connid}: got header line {l!r}')
			# end of headers?
			if l == bytes ():
				break
			try:
				key, value = l.decode ('utf-8').split (':', 1)
				headers.add (key.strip (), value.strip ())
			except ValueError:
				logger.error (f'cannot parse {l}')

		logger.debug (f'{connid}: {rawPath} {method} got headers {headers}')

		route = None
		try:
			netloc = headers['host']
			reqUrl = reqUrl.set (scheme='http', netloc=netloc)
			logger.debug (f'got request url {reqUrl}')
			routeKey = None
			for d in self.domain:
				m = parse (d, reqUrl.netloc)
				if m is not None:
					routeKey = RouteKey (key=m['key'], user=m['user'])
					logger.debug (f'{connid}: got route key {routeKey}')
					break
			route = self.routes[routeKey]
		except (KeyError, ValueError):
			logger.info (f'{connid}: cannot find route for {reqUrl}')
			self.status['noroute'] += 1
			# error is written to client later

		# is this a non-forwarded request?
		segments = reqUrl.path.segments
		if len (segments) > 0 and segments[0] == '_conductor':
			if segments[1] == 'auth':
				logger.info (f'authorization request for {reqUrl.netloc}')
				try:
					nextLoc = reqUrl.query.params['next'].encode ('utf-8')
				except KeyError:
					nextLoc = b'/'
				writer.write (b'\r\n'.join ([
						b'HTTP/1.0 302 Found',
						b'Location: ' + nextLoc,
						b'Set-Cookie: authorization=' + segments[2].encode ('utf-8') + b'; HttpOnly; Path=/',
						b'Cache-Control: no-store',
						b'',
						b'Follow the white rabbit.']))
			elif segments[1] == 'status':
				writer.write (b'HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n')
				self.status['routesTotal'] = len (self.routes)
				writer.write (json.dumps (self.status, ensure_ascii=True).encode ('ascii'))
			else:
				writer.write (b'HTTP/1.0 404 Not Found\r\nContent-Type: plain/text\r\n\r\nNot found')
			writer.close ()
			return

		if not route:
			writer.write (b'HTTP/1.0 404 Not Found\r\nConnection: close\r\n\r\n')
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
			# Only hashed authorization is available to server.
			if c.key == 'authorization' and self.hashKey (c.value) == route.auth:
				authorized = True
				break
		try:
			# do not forward auth cookie to the application, so it can’t leak it.
			del cookies['authorization']
			headers['Cookie'] = cookies.output (header='', sep='')
		except KeyError:
			# nonexistent cookie is fine
			pass

		if not authorized:
			logger.info (f'{connid}-{reqUrl}: not authorized, cookies sent {cookies.values()}')
			writer.write (b'HTTP/1.0 403 Unauthorized\r\nContent-Type: plain/text\r\nConnection: close\r\n\r\nUnauthorized')
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
			logger.info (f'{connid}-{reqUrl}: route {routeKey} is broken')
			writer.write (b'HTTP/1.0 502 Bad Gateway\r\nConnection: close\r\n\r\n')
			writer.close ()
			self.status['broken'] += 1
			return

		# some headers are fixed
		# not parsing body, so we cannot handle more than one request per connection
		# XXX: this is super-inefficient
		if 'Upgrade' not in headers or headers['Upgrade'].lower () != 'websocket':
			headers['Connection'] = 'close'

		# write http banner plus headers
		sockwriter.write (method + b' ' + rawPath + b' ' + proto + b'\r\n')
		for k, v in headers.items ():
			sockwriter.write (f'{k}: {v}\r\n'.encode ('utf-8'))
		sockwriter.write (b'\r\n')

		async def beforeABClose (result):
			if result == 0:
				# no response received from client
				logger.info (f'{connid}-{reqUrl}: route {routeKey} got no result from server')
				writer.write (b'HTTP/1.0 502 Bad Gateway\r\nConnection: close\r\n\r\n')

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

class ClientInterface:
	__slots__ = ('runtimeDir', 'socketsDir', 'proxy')

	def __init__ (self, runtimeDir, proxy):
		self.runtimeDir = runtimeDir
		self.socketsDir = None
		self.proxy = proxy

	@staticmethod
	async def _validateSocket (path, uid):
		# it is safe to stat and then connect (no race connection;
		# otherwise we’d have to connect and then fstat), because
		# the sticky bit is set on the parent directory and no-one
		# else can unlink the socket.
		sstat = os.stat (path, follow_symlinks=False)
		permissions = stat.S_IMODE (sstat.st_mode)
		# neither world-read nor writeable
		if permissions & 0o6:
			raise Exception (f'{path} world-read- or -writeable')
		# same owner
		if uid != sstat.st_uid:
			raise Exception (f'{path} socket owner does not match')
		if not stat.S_ISSOCK (sstat.st_mode):
			raise Exception (f'{path} not a socket')
		# check socket is connect()’able
		await asyncio.open_unix_connection (path=path)

	async def _handleLine (self, reader, writer, uid, username, socketDir, routes):
		response = dict (status='error')
		try:
			l = await reader.readline ()
			if not l:
				return False
			config = json.loads (l)
			logger.debug (f'got config {config} from {username}')

			# make sure noone else can write to that dir if we received
			# the config, client should be listening on the socket by
			# now.
			os.chmod (socketDir, 0o770)

			socketPath = os.path.join (socketDir, 'socket')
			await self._validateSocket (socketPath, uid)

			key = RouteKey (key=str (config.get ('key')).lower(), user=username.lower())
			route = Route (socket=socketPath, key=key, auth=config['auth'])

			self.proxy.addRoute (route)
			routes.append (route)
		except Exception as e:
			response = dict (status='error', reason=e.args[0])
			return False
		else:
			response = dict (status='ok',
					urls=[d.format (key=key.key, user=key.user) for d in self.proxy.domain])
			return True
		finally:
			writer.write (json.dumps (response).encode ('utf-8'))
			writer.write (b'\n')
			try:
				await writer.drain ()
			except ConnectionResetError:
				pass

	async def _accept (self, reader, writer):
		sock = writer.get_extra_info ('socket')
		creds = sock.getsockopt (socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize('3i'))
		pid, uid, gid = struct.unpack('3i',creds)
		remoteUser = pwd.getpwuid (uid)
		username = remoteUser.pw_name
		routes = []

		with tempfile.TemporaryDirectory (prefix=f'{username}-', dir=self.socketsDir) as socketDir:
			os.chmod (socketDir, stat.S_ISGID | stat.S_ISVTX | 0o777)
			banner = dict (directory=socketDir, status='ok')
			logger.debug (f'Writing banner {banner}')
			writer.write (json.dumps (banner).encode ('utf-8'))
			writer.write (b'\n')
			await writer.drain ()

			await self._handleLine (reader, writer, uid, username, socketDir, routes)
			await reader.readline ()

			for r in routes:
				logger.debug (f'Removing route {r}')
				self.proxy.deleteRoute (r.key)
			writer.close ()
			try:
				await writer.wait_closed ()
			except BrokenPipeError:
				pass

	async def run (self):
		self.socketsDir = os.path.join (self.runtimeDir, 'sockets')
		if not os.path.isdir (self.socketsDir):
			# make sure clients can access (x) the directory, but not list it
			os.makedirs (self.socketsDir, mode=0o771)
		os.chmod (self.socketsDir, 0o771)

		listenPath = os.path.join (self.runtimeDir, 'client')
		async with await asyncio.start_unix_server (self._accept, listenPath) as server:
			# everyone can write
			os.chmod (listenPath, 0o777)
			await server.serve_forever ()

def main (): # pragma: nocover
	parser = argparse.ArgumentParser(description='conductor server part')
	parser.add_argument ('-p', '--port', default=8888, type=int,
		metavar='PORT', help='Listen port')
	parser.add_argument ('-s', '--sock', default=None, metavar='PATH',
		help='Listen port')
	parser.add_argument ('-v', '--verbose', action='store_true', help='Verbose output')
	parser.add_argument ('-r', '--runtime-dir', dest='runtimeDir', default='/var/run/conductor', help='Runtime data directory')
	parser.add_argument ('-d', '--domain', default=[], action='append', metavar='DOMAIN', help='Domain match pattern')

	args = parser.parse_args()
	if args.verbose:
		logging.basicConfig (level=logging.DEBUG)
	else:
		logging.basicConfig (level=logging.INFO)

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
	client = ClientInterface (os.path.abspath (args.runtimeDir), proxy)
	loop.run_until_complete (client.run ())

