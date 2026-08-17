"""Setup script for fshub package"""

from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="fshub",
    version="0.2.0",
    author="fshub contributors",
    description="File System Hub for managing files across devices",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/yourusername/fshub",
    packages=find_packages(exclude=("tests", "tests.*")),
    include_package_data=True,
    package_data={"fshub": ["templates/*.html", "static/*"]},
    classifiers=[
        "Development Status :: 3 - Alpha",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
    ],
    python_requires=">=3.8",
    install_requires=[
        "click>=8.0.0",
        "Flask>=2.0.0",
        "PyYAML>=6.0",
    ],
    extras_require={
        "sysinfo": ["psutil>=5.8.0"],
        "dev": ["pytest>=7.0"],
    },
    entry_points={
        "console_scripts": [
            "fshub=fshub.main:cli",
        ],
    },
)
