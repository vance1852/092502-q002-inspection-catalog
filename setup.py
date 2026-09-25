from setuptools import find_packages, setup

setup(
    name="inspection-catalog-core",
    version="0.1.0",
    description="家具工艺巡查资料服务",
    package_dir={"": "src"},
    packages=find_packages("src"),
    python_requires=">=3.11",
)
