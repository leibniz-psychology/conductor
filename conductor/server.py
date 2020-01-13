import asyncio, socket, os, struct, stat, json, logging, argparse
from http.cookies import BaseCookie

import aionotify
from yarl import URL
from multidict import CIMultiDict

from .nss import getUser
from .util import copy

routes = {}

async def connectCb (reader, writer):
	# simple HTTP parsing
	l = (await reader.readline ()).rstrip (b'\r\n')
	method, path, proto = l.split (b' ')
	headers = CIMultiDict ()
	while True:
		if len (headers) > 100:
			logging.error ('too many headers')
			writer.write (b'HTTP/1.0 500 Server Error\r\n\r\n')
			writer.close ()
			return

		l = (await reader.readline ()).rstrip (b'\r\n')
		# end of headers?
		if l == b'\r\n' or not l:
			break
		try:
			key, value = l.decode ('utf-8').split (':', 1)
			headers.add (key.strip (), value.strip ())
		except ValueError:
			logging.error (f'cannot parse {l}')

	logging.debug (f'{path} {method} got headers {headers}')

	try:
		host, _ = headers['host'].split ('.', 1)
		route = routes[host]
	except KeyError:
		logging.info (f'cannot find route for host {host}')
		# XXX: add url as parameter to the redirect url, so the target can then
		# redirect back after starting the instance
		url = URL ('http://localhost:8000/') / 'project' / host / 'start'
		writer.write (b'HTTP/1.0 302 Found\r\nLocation: ' + str (url).encode ('utf8') + b'\r\n\r\n')
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
		if c.key == 'authorization' and c.value == route['auth']:
			authorized = True
	try:
		del cookies['authorization']
		headers['Cookie'] = cookies.output (header='', sep='')
	except KeyError:
		# nonexistent cookie is fine
		pass

	if not authorized:
		logging.info (f'not authorized, cookies sent {cookies.values()}')
		writer.write (b'HTTP/1.0 403 Unauthorized\r\nContent-Type: plain/text\r\n\r\nUnauthorized')
		writer.close ()
		return			

	# try opening the socket
	try:
		sockreader, sockwriter = await asyncio.open_unix_connection (path=route['socket'])
	except (ConnectionRefusedError, FileNotFoundError, PermissionError):
		logging.info (f'route is broken')
		del routes[host]
		# XXX: add url as parameter to the redirect url, so the target can then
		# redirect back after starting the instance
		url = URL ('http://localhost:8000/') / 'project' / host / 'start'
		writer.write (b'HTTP/1.0 302 Found\r\nLocation: ' + str (url).encode ('utf8') + b'\r\n\r\n')
		writer.close ()
		return

	# some headers are fixed
	# not parsing body, so we cannot handle more than one request per connection
	# XXX: this is super-inefficient
	if 'Upgrade' not in headers or headers['Upgrade'].lower () != 'websocket':
		headers['Connection'] = 'close'
	
	# write http banner plus headers
	sockwriter.write (method + b' ' + path + b' ' + proto + b'\r\n')
	for k, v in headers.items ():
		sockwriter.write (f'{k}: {v}\r\n'.encode ('utf-8'))
	sockwriter.write (b'\r\n')
	# then copy the body
	a = asyncio.ensure_future (copy (sockreader, writer))
	b = asyncio.ensure_future (copy (reader, sockwriter))
	await asyncio.wait ([a, b])

async def proxy (port):
	"""
	Start public proxy worker
	"""

	logging.info (f'starting server on port {port}')
	server = await asyncio.start_server (connectCb, port=port)
	await server.serve_forever ()

async def addConfig (forestPath, f):
	"""
	Add file f relative to forestPath to proxy configuration
	"""

	configFile = os.path.join (forestPath, f)
	if not configFile.endswith ('.json'):
		# skip files that are not configs
		logging.error (f'{configFile} does not end with .json')
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
		socketPath = os.path.realpath (os.path.join (forestPath, o['socket']))
		if not os.path.dirname (socketPath).startswith (forestPath):
			raise Exception (f'{socketPath} not in forest dir {forestPath}')
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
		route = {'socket': socketPath, 'key': user['name'], 'auth': o['auth']}

		# XXX: delete old config?
		routes[route['key']] = route

		logging.info (f'configured route {route["key"]} → {route["socket"]}')
	except Exception as e:
		logging.error (e)
		#os.unlink (configFile)

async def watch (loop, forestPath):
	"""
	Start directory watcher worker
	"""

	forestPath = os.path.realpath (forestPath)

	# make sure the forest dir has appropriate permissions
	fstat = os.stat (forestPath, follow_symlinks=False)
	permissions = stat.S_IMODE (fstat.st_mode)
	expect = 0o3713
	if permissions != expect:
		logging.error (f'Permissions on forest directory {forestPath} are not correct. Is 0{permissions:o} should be 0{expect:o}')
		return

	# add existing files
	logging.info (f'reading existing configs in {forestPath}')
	for f in os.listdir (forestPath):
		await addConfig (forestPath, f)

	logging.info ('starting watcher')
	watcher = aionotify.Watcher()
	watcher.watch (path=forestPath, flags=aionotify.Flags.MOVED_TO)

	await watcher.setup(loop)
	while True:
		event = await watcher.get_event()
		if event.flags == aionotify.Flags.MOVED_TO:
			await addConfig (forestPath, event.name)
		else:
			assert False
	watcher.close ()

def main ():
	logging.basicConfig (level=logging.INFO)
	parser = argparse.ArgumentParser(description='conductor server part')
	parser.add_argument ('-p', '--port', default=8888, type=int, help='Listen port')
	parser.add_argument ('forest', default='forest', help='Local forest path')

	args = parser.parse_args()

	loop = asyncio.get_event_loop ()
	loop.create_task (proxy (args.port))
	loop.run_until_complete (watch (loop, args.forest))

