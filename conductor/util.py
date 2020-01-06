"""
Miscellaneous utility functions
"""

async def copy (source, dest):
	try:
		while True:
			buf = await source.read (4096)
			if not buf:
				dest.close ()
				break
			foo = dest.write (buf)
			assert foo is None
	except ConnectionResetError:
		# this is fine
		pass

