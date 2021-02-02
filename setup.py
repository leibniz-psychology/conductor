from setuptools import setup

setup(
    name='conductor',
    version='0.1',
    author='Lars-Dominik Braun',
    author_email='ldb@leibniz-psychology.org',
    url='https://github.com/leibniz-psychology/conductor',
    packages=['conductor'],
    description='UNIX domain socket forwarding and multiplexing',
    #long_description=open('README.rst').read(),
    long_description_content_type='text/x-rst',
    install_requires=[
		'asyncssh',
		'multidict',
		'furl',
		'parse',
    ],
    python_requires='>=3.7',
    entry_points={
    'console_scripts': [
            'conductor-server = conductor.server:main',
            'conductor = conductor.client:main',
            ],
    },
    classifiers = [
        'License :: OSI Approved :: MIT License',
        'Development Status :: 4 - Beta',
        'Operating System :: POSIX :: Linux',
        'Programming Language :: Python :: 3',
        'Topic :: Internet :: Proxy Servers',
        ],
)
