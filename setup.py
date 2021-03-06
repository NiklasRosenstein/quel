
import setuptools
import sys

with open('README.md', encoding='utf8') as fp:
  readme = fp.read()

setuptools.setup(
  name = 'quel',
  version = '0.9.1',
  author = 'Niklas Rosenstein',
  author_email = 'rosensteinniklas@gmail.com',
  python_requires = '>=3.7',
  #install_requires = [],
  packages = setuptools.find_packages('src'),
  package_dir = {'': 'src'},
)
