import pytest

from .client import Client, parseSSHPath
from .test_util import socketServer

@pytest.mark.asyncio
async def test_checkSocketExists_yes (socketServer):
	with pytest.raises (FileExistsError):
		Client.checkSocketExists (socketServer)

def test_checkSocketExists_no ():
	Client.checkSocketExists ('/nonexistent')

def test_parseSSHPath ():
	assert parseSSHPath ('user@host:/path') == ('user', 'host', '/path')
	assert parseSSHPath ('user@host:/path@/path') == ('user', 'host', '/path@/path')
	assert parseSSHPath ('user@host:/path:/path') == ('user', 'host', '/path:/path')

	assert parseSSHPath ('host:/path') == (None, 'host', '/path')

