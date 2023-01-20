# Copyright 2019–2022 Leibniz Institute for Psychology
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

import asyncio, socket, os, struct, stat, json, argparse, functools, \
		traceback, time, tempfile, pwd
from http.cookies import BaseCookie
from collections import namedtuple
from hashlib import blake2b
from furl import furl
import structlog

from multidict import CIMultiDict
from parse import parse

from .util import proxy, configureStructlog

class BadRequest (Exception):
	pass

def handleException (func):
	""" XXX: Should probably use some http library here? """
	@functools.wraps(func)
	async def wrapped(*args):
		# XXX: get the logger from somewhere else
		logger = structlog.get_logger ()
		 # Some fancy foo stuff
		try:
			return await func(*args)
		except BadRequest as e:
			self, reader, writer = args
			logger.error ('bad_request', error=e)
			writer.write (b'HTTP/1.0 400 Bad Request\r\n\r\n')
			writer.close ()
		except Exception as e:
			self, reader, writer = args
			traceback.print_exc()
			logger.error ('exception', error=e)
			writer.write (b'HTTP/1.0 500 Server Error\r\n\r\n')
			writer.close ()
	return wrapped

# a route from key.domain to socket with auth
Route = namedtuple ('Route', ['socket', 'key', 'auth'])
RouteKey = namedtuple ('RouteKey', ['key', 'user'])

class Conductor:
	__slots__ = ('routes', 'domain', 'nextConnId', 'status', 'logger')

	def __init__ (self, domain):
		self.logger = structlog.get_logger ()
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
			try:
				writer.close ()
				await writer.wait_closed ()
			except Exception as e:
				# ignore any errors while closing
				pass
			self.status['requestActive'] -= 1

	@staticmethod
	def hashKey (v):
		return blake2b (v.encode ('utf-8')).hexdigest ()

	@handleException
	async def handleRequest (self, reader, writer):
		# connection id, for debugging
		connid = self.nextConnId
		self.nextConnId += 1

		logger = self.logger.bind (connid=connid)
		logger.debug ('new_connection')

		# simple HTTP parsing, yes this is a terrible idea.
		l = await reader.readline ()
		if l == bytes ():
			raise BadRequest (f'{connid}: unexpected eof')
		l = l.rstrip (b'\r\n')
		try:
			method, rawPath, proto = l.split (b' ')
			logger.debug ('http_request', method=method, rawPath=rawPath, proto=proto)
		except ValueError:
			logger.error ('http_request_split_error', line=l)
			raise
		reqUrl = furl (rawPath.decode ('utf-8'))

		headers = CIMultiDict ()
		while True:
			if len (headers) > 100:
				raise BadRequest ('too_many_headers')

			l = await reader.readline ()
			if l == bytes ():
				raise BadRequest (f'{connid}: unexpected eof in headers')
			l = l.rstrip (b'\r\n')
			logger.debug ('http_header_line', line=l)
			# end of headers?
			if l == bytes ():
				break
			try:
				key, value = l.decode ('utf-8').split (':', 1)
				headers.add (key.strip (), value.strip ())
			except ValueError:
				logger.error ('http_header_split_error', line=l)

		# Do not add path, add URL later.
		logger = logger.bind (method=method.decode ('utf-8', errors='ignore'),
				headers=list (headers.items ()))
		logger.debug ('parsed_request', path=rawPath)

		route = None
		try:
			netloc = headers['host']
			reqUrl = reqUrl.set (scheme='http', netloc=netloc)
			logger = logger.bind (url=str (reqUrl))
			logger.debug ('request_url')
			routeKey = None
			for d in self.domain:
				m = parse (d, reqUrl.netloc)
				if m is not None:
					routeKey = RouteKey (key=m['key'], user=m['user'])
					logger.debug ('route_key', routeKey=routeKey)
					break
			route = self.routes[routeKey]
			logger = logger.bind (route=route)
		except (KeyError, ValueError):
			logger.info ('no_such_route')
			self.status['noroute'] += 1
			# error is written to client later

		# is this a non-forwarded request?
		segments = reqUrl.path.segments
		if len (segments) > 0 and segments[0] == '_conductor':
			if segments[1] == 'auth':
				logger.info ('authorization_request')
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
			logger.info ('unauthorized', cookies=cookies)
			writer.write (b'HTTP/1.0 403 Unauthorized\r\nContent-Type: plain/text\r\nConnection: close\r\n\r\nUnauthorized')
			writer.close ()
			self.status['unauthorized'] += 1
			return

		# try opening the socket
		try:
			sockreader, sockwriter = await asyncio.open_unix_connection (path=route.socket)
		except (ConnectionRefusedError, FileNotFoundError, PermissionError):
			logger.info ('route_broken')
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
				logger.info ('empty_reply_from_server')
				writer.write (b'HTTP/1.0 502 Bad Gateway\r\nConnection: close\r\n\r\n')

		await proxy ((sockreader, sockwriter, 'sock'), (reader, writer, 'web'), logger=logger, beforeABClose=beforeABClose)

	async def run (self, port=None, sock=None, sslctx=None):
		"""
		Start public proxy worker
		"""

		self.logger.info ('start', port=port, domains=self.domain)
		server = await asyncio.start_server (self._connectCb, port=port, sock=sock, ssl=sslctx)
		await server.serve_forever ()

	def addRoute (self, route):
		assert isinstance (route.key, RouteKey)
		if route.key in self.routes:
			raise KeyError ('exists')
		self.routes[route.key] = route
		self.logger.info ('route_add', route=route)

	def deleteRoute (self, key):
		route = self.routes.pop (key)
		self.logger.info ('route_remove', route=route)

class ClientInterface:
	__slots__ = ('runtimeDir', 'socketsDir', 'proxy', 'logger')

	def __init__ (self, runtimeDir, proxy):
		self.runtimeDir = runtimeDir
		self.socketsDir = None
		self.proxy = proxy
		self.logger = structlog.get_logger ()

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

	async def _handleLine (self, reader, writer, uid, username, socketDir, routes, logger):
		response = dict (status='error')
		try:
			l = await reader.readline ()
			if not l:
				return False
			config = json.loads (l)
			logger.debug ('config_from_user', config=config)

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

		logger = self.logger.bind (pid=pid, uid=uid, gid=gid, username=username)
		logger.debug ('client_connect')

		with tempfile.TemporaryDirectory (prefix=f'{username}-', dir=self.socketsDir) as socketDir:
			os.chmod (socketDir, stat.S_ISGID | stat.S_ISVTX | 0o777)
			banner = dict (directory=socketDir, status='ok')
			logger.debug ('banner', banner=banner)
			writer.write (json.dumps (banner).encode ('utf-8'))
			writer.write (b'\n')
			await writer.drain ()

			await self._handleLine (reader, writer, uid, username, socketDir, routes, logger=logger)
			await reader.readline ()

			for r in routes:
				logger.debug ('client_route_remove', route=r)
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
	parser.add_argument ('--ssl-cert-file', dest='sslCertFile', metavar='FILE', help='Public SSL certificate')
	parser.add_argument ('--ssl-key-file', dest='sslKeyFile', metavar='FILE', help='Private SSL key')
	parser.add_argument ('--ssl-allowed-clients', dest='sslAllowedClients', metavar='FILE', help='Authorized client certificates')

	args = parser.parse_args()

	configureStructlog (args.verbose)

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

	if args.sslCertFile and args.sslKeyFile:
		import ssl
		sslctx = ssl.create_default_context (ssl.Purpose.CLIENT_AUTH)
		if args.sslAllowedClients:
			sslctx.verify_mode = ssl.CERT_REQUIRED
			sslctx.load_verify_locations(cafile=args.sslAllowedClients)
		sslctx.load_cert_chain (certfile=args.sslCertFile, keyfile=args.sslKeyFile)
	else:
		sslctx = None

	loop = asyncio.get_event_loop ()
	proxy = Conductor (args.domain)
	loop.create_task (proxy.run (port, sock, sslctx))
	client = ClientInterface (os.path.abspath (args.runtimeDir), proxy)
	loop.run_until_complete (client.run ())

