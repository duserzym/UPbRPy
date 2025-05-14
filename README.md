# UPbRPy

U-Pb data (R)eduction engine in Python for LA-ICP-MS data. Currently supports data collected with a Thermo iCAP RQ ICP-Ms coupled to a Teledyne Analyte Excite+ 193 nm excimer laser ablation system. 

## About
UPbRPy is developed with a goal of providing a Python-based reproducible data reduction and final result visualization workflow for U-Pb LA-ICP-MS data. The package features:

**1) fast data reduction with no limits on individual spot analyses duration and spot numbers per experiment session**\
**2) elemental abundance analysis and isotopic analysis from the same spots**\
**3) downhole isotope fractionation correction**\
**4) uncertainty propagation at the sweep-level (for depth profiles) and spot-level (for conventional spot means)**\
**5) interactive data visualization throughout the data reduction process**\
**6) export depth profile data**\
**7) making publication-ready figures**


### interactive time series visualization
<img src="./examples/example_time_series.png" alt="Interactive time series visualization" width="600">

### Elemental concentration calibration
<img src="./examples/example_concentration_calibration.png" alt="Elemental concentration calibration" width="550">

### Dowwhole fractionation characterization and correction
<img src="./examples/example_downhole_fractionation.png" alt="Dowwhole fractionation characterization and correction" width="600">

### Session-wide fractionation correction
<img src="./examples/example_session_correction.png" alt="Session-wide fractionation correction" width="600">

### Concordia diagram visualization
- example Plešovice zircon LA-ICP-MS data

<img src="./examples/example_Wetherill_concordia.png" alt="Wetherill Concordia diagram" width="500">

## Installation
pip release will be made available. During the development stage, you can download or clone this repository and add the path to the `UPbRPy` folder to your Python path by adding the following lines to your script:
```python
import sys
import os
sys.path.append(os.path.join(os.path.dirname(__file__), 'PATH_TO_UPbRPy'))
```
where `PATH_TO_UPbRPy` is the absolute path to the `UPbRPy` folder. 

Alternatively, you can add the path to the `UPbRPy` folder to your Python path in your environment variables. For MacOS and Linux, you can do this by adding the following line to your `.bash_profile` or `.bashrc` or `.zshrc` file:  
```bash
export PYTHONPATH=$PYTHONPATH:/path/to/UPbRPy
```
For Windows, I assume you can do this by adding the following line to your `Environment Variables`:
```bash
set PYTHONPATH=%PYTHONPATH%;C:\path\to\UPbRPy
```

### Input file format
This data reduction engine takes individual laser spot analysis file from a single experiment session exported from Qtegra software. Go to `Export data` -> Select exprter: `Laser Data Reduction Export` -> Select all available analytes -> Export.

### Bug reports and issue discussions
I write bugs. And the only way to deal with this is to keep thinking about the functions and keep fixing them. If you find any issues with the code, please let me know. You can do this by submitting an issue on the [Issues](https://github.com/duserzym/UPbRPy/issues) page of this repository. It'd be lovely if you can provide a minimal example that reproduces the bug.