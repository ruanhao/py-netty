# setup.py
from setuptools import setup, find_packages
from pathlib import Path

this_directory = Path(__file__).parent
long_description = (this_directory / "README.md").read_text()
# install_requires = (this_directory / 'requirements.txt').read_text().splitlines()

version = '1.0.2'

config = {
    'name': 'py-netty',
    'url': 'https://github.com/ruanhao/py-netty',
    'license': 'MIT',
    "long_description": long_description,
    "long_description_content_type": 'text/markdown',
    'description': 'TCP framework in flavor of Netty',
    'author' : 'Hao Ruan',
    'author_email': 'ruanhao1116@gmail.com',
    'keywords': ['network', 'tcp', 'non-blocking', 'epoll', 'nio', 'netty'],
    'version': version,
    'packages': find_packages(),
    'install_requires': ['attrs'],
    'python_requires': ">=3.7, <4",
    'setup_requires': ['wheel'],
    'classifiers': [
        "Intended Audience :: Developers",
        'License :: OSI Approved :: MIT License',
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3 :: Only",
        "Topic :: Software Development :: Libraries",
    ],
}

setup(**config)
