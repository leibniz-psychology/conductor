# Copyright 2021 Leibniz Institute for Psychology
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

"""
Some basic server functionality tests, no integration tests with client.
"""

import asyncio, json, os, getpass, socket
from unittest.mock import MagicMock
from tempfile import TemporaryDirectory

import pytest, aiohttp
from aiohttp import web
from furl import furl

from .server import ClientInterface, Route, RouteKey, Conductor

@pytest.fixture
async def clientInterface ():
	proxy = MagicMock ()
	proxy.addRoute = MagicMock ()
	proxy.deleteRoute = MagicMock ()

	with TemporaryDirectory () as runtimeDir:
		c = ClientInterface (runtimeDir, proxy)
		task = asyncio.ensure_future (c.run ())
		await asyncio.sleep (0.5)

		yield proxy, runtimeDir

		task.cancel ()
		with pytest.raises (asyncio.CancelledError):
			await task

@pytest.mark.asyncio
async def test_client_interface_no_socket (clientInterface):
	""" Connect, but create no socket """

	proxy, runtimeDir = clientInterface

	clientSocket = os.path.join (runtimeDir, 'client')
	reader, writer = await asyncio.open_unix_connection (path=clientSocket)
	l = await reader.readline ()
	o = json.loads (l.decode ('utf-8'))
	assert o['status'] == 'ok', o
	assert os.path.isdir (o['directory'])

	writer.write (json.dumps (dict (auth='123', key='foobar')).encode ('utf-8'))
	writer.write (b'\n')
	await writer.drain ()

	proxy.addRoute.assert_not_called ()
	proxy.deleteRoute.assert_not_called ()

	writer.close ()
	await writer.wait_closed ()

@pytest.mark.asyncio
async def test_client_interface_ok (clientInterface):
	""" Connect, but create no socket """

	proxy, runtimeDir = clientInterface

	clientSocket = os.path.join (runtimeDir, 'client')
	reader, writer = await asyncio.open_unix_connection (path=clientSocket)
	l = await reader.readline ()
	o = json.loads (l.decode ('utf-8'))
	assert o['status'] == 'ok', o
	assert os.path.isdir (o['directory'])
	remoteSocket = os.path.join (o['directory'], 'socket')

	async def _accept (reader, writer):
		pass

	async with await asyncio.start_unix_server (_accept, remoteSocket) as server:
		os.chmod (remoteSocket, 0o660)
		task = asyncio.create_task (server.serve_forever ())

		writer.write (json.dumps (dict (auth='123', key='foobar')).encode ('utf-8'))
		writer.write (b'\n')
		await writer.drain ()

		l = await reader.readline ()
		o = json.loads (l.decode ('utf-8'))
		assert o['status'] == 'ok', o

		key = RouteKey (key='foobar', user=getpass.getuser ())
		expectedRoute = Route (key=key, auth='123', socket=remoteSocket)
		proxy.addRoute.assert_called_once_with (expectedRoute)
		proxy.deleteRoute.assert_not_called ()

		writer.close ()
		await writer.wait_closed ()

		# give the server time to remove the route
		await asyncio.sleep (0.5)

		proxy.deleteRoute.assert_called_once_with (key)

		task.cancel ()
		with pytest.raises (asyncio.CancelledError):
			await task

async def hello(request):
    return web.Response (text="Hello, world")

@pytest.fixture
async def simpleWebServer ():
	with TemporaryDirectory () as socketDir:
		app = web.Application()
		app.add_routes([web.get('/', hello)])
		
		socketPath = os.path.join (socketDir, 'socket')
		runner = web.AppRunner(app)
		await runner.setup()
		site = web.UnixSite (runner, socketPath)
		await site.start ()

		yield socketPath, runner

		# destroy application
		await runner.cleanup()

@pytest.fixture
async def conductor ():
	with TemporaryDirectory () as socketDir:
		serverSocketPath = os.path.join (socketDir, 'server')
		sock = socket.socket (socket.AF_UNIX)
		sock.bind (serverSocketPath)

		user = getpass.getuser ()
		c = Conductor (['{key}-{user}.conductor.local'])

		task = asyncio.create_task (c.run (sock=sock))

		yield serverSocketPath, c

		task.cancel ()
		with pytest.raises (asyncio.CancelledError):
			await task

@pytest.mark.asyncio
async def test_conductor (conductor, simpleWebServer):
	serverSocketPath, c = conductor
	socketPath, runner = simpleWebServer

	user = getpass.getuser ()
	key = 'foobar'
	auth = '123'

	conn = aiohttp.UnixConnector(path=serverSocketPath)
	async with aiohttp.ClientSession(connector=conn) as session:
		async with session.get(f'http://{key}-{user}.conductor.local/_conductor/status') as resp:
			assert resp.status == 200
			o = await resp.json ()
			assert o['routesTotal'] == 0
			assert o['requestTotal'] == 1
			assert o['requestActive'] == 1
			assert o['noroute'] == 1

		# make sure that requests are properly counted and requestActive is decreased
		reader, writer = await asyncio.open_unix_connection (path=serverSocketPath)
		writer.write (b'invalid http request\n')
		writer.close ()
		await writer.wait_closed ()
		async with session.get(f'http://{key}-{user}.conductor.local/_conductor/status') as resp:
			assert resp.status == 200
			o = await resp.json ()
			assert o['requestTotal'] == 3
			assert o['requestActive'] == 1

		async with session.get(f'http://{key}-{user}.conductor.local/_conductor/nonexistent') as resp:
			assert resp.status == 404

		async with session.get(f'http://{key}-{user}.conductor.local/') as resp:
			assert resp.status == 404

		routeKey = RouteKey (key=key, user=user)
		route = Route (key=routeKey, auth=c.hashKey (auth), socket=socketPath)
		c.addRoute (route)

		for u in (f'http://nonexistent-{user}.conductor.local', 'http://invalidpattern.conductor.local', 'http://different.domain'):
			async with session.get(u) as resp:
				assert resp.status == 404

		async with session.get(f'http://{key}-{user}.conductor.local') as resp:
			assert resp.status == 403
		# add unrelated cookie
		session.cookie_jar.update_cookies ({'unrelated': 'value'})
		async with session.get(f'http://{key}-{user}.conductor.local') as resp:
			assert resp.status == 403

		async with session.get(f'http://{key}-{user}.conductor.local/_conductor/auth/{auth}') as resp:
			assert resp.status == 200
			assert await resp.text() == 'Hello, world'

		async with session.get(f'http://{key}-{user}.conductor.local/_conductor/auth/{auth}?next=/nonexistent') as resp:
			assert resp.status == 404
			assert furl (resp.url).path == '/nonexistent'

		# big request
		headers = dict ([(f'key-{i}', 'val') for i in range (101)])
		async with session.get(f'http://{key}-{user}.conductor.local/', headers=headers) as resp:
			assert resp.status == 400

		# destroy application
		await runner.cleanup()

		async with session.get(f'http://{key}-{user}.conductor.local/') as resp:
			assert resp.status == 502

		c.deleteRoute (routeKey)

		async with session.get(f'http://{key}-{user}.conductor.local/') as resp:
			assert resp.status == 404

