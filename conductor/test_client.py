import pytest

from .client import Client
from .test_util import socketServer

@pytest.mark.asyncio
async def test_checkSocketExists_yes (socketServer):
	with pytest.raises (FileExistsError):
		Client.checkSocketExists (socketServer)

def test_checkSocketExists_no ():
	Client.checkSocketExists ('/nonexistent')

