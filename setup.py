from setuptools import setup

setup(
    name='conductor',
    version='0.1',
    author='Lars-Dominik Braun',
    author_email='ldb@leibniz-psychology.org',
    #url='https://',
    packages=['conductor'],
    #license='LICENSE.txt',
    description='UNIX domain socket forwarding and multiplexing',
    #long_description=open('README.rst').read(),
    long_description_content_type='text/x-rst',
    install_requires=[
		'asyncssh',
		'aionotify',
		'multidict',
		'furl',
		'parse',
    ],
    python_requires='>=3.7',
    entry_points={
    'console_scripts': [
            'conductor-server = conductor.server:main',
            'conductor = conductor.client:main',
            'conductor-pipe = conductor.client:pipe',
            ],
    },
)
