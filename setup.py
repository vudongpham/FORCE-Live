from setuptools import setup, find_packages

setup(
    name="forcelive",
    version="1.0",
    packages=find_packages(),
    install_requires=[
        "rasterio",
        "pyproj",
        "Flask"
    ],

    package_data={
        "forcelive": [
            "templates/**/*",
            "static/**/*",
        ]
    },
    
    python_requires=">=3.7",
    entry_points={
        "console_scripts": [
            "forcelive=forcelive.app:main",
        ],
    },
)
