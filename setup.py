#!/usr/bin/env python3
from setuptools import setup, find_packages

setup(
    name="rossum",
    version="3.14.0",
    description="Command line interface for controlling the Rossum platform",
    long_description=open("README.md").read(),
    long_description_content_type="text/markdown",
    url="https://developers.rossum.ai/",
    author="Rossum developers",
    author_email="support@rossum.ai",
    license="MIT",
    project_urls={
        "Source": "https://github.com/rossumai/rossum",
        "Tracker": "https://github.com/rossumai/rossum/issues",
    },
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Intended Audience :: System Administrators",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
    ],
    packages=find_packages(exclude=("tests*",)),
    install_requires=[
        "pandas",
        "click<8.1.0",
        "click-shell",
        'xlrd > 1.2.0;python_version>"3.6"',
        'xlrd==1.2.0;python_version=="3.6"',
        "requests",
        "jsondiff",
        "tabulate",
        'dataclasses;python_version<"3.7"',
        "openpyxl>=2.6",
        "jmespath",
        "polling2",
        "more_itertools",
        "tenacity",
    ],
    python_requires=">=3.6",
    setup_requires=["pytest-runner"],
    tests_require=[
        "pytest<6.1.2",  # higher versions support only python3.7+
        "pytest-cov",
        "requests_mock",
        "pytest-click",
        'tomli<2.0;python_version<"3.7"',
    ],
    zip_safe=False,
    entry_points={"console_scripts": ["rossum = rossum.main:entry_point"]},
)
