"""Backward-compatible façade for the former monolithic module.

New code may import from the focused modules directly. Existing imports from
``UPbRPy.UPbRPy`` remain supported.
"""

from .constants import *  # noqa: F401,F403
from .standards import *  # noqa: F401,F403
from .io import *  # noqa: F401,F403
from .visualization import *  # noqa: F401,F403
from .mpl_concordia import *  # noqa: F401,F403
from .concentration import *  # noqa: F401,F403
from .ratios import *  # noqa: F401,F403
from .isoplot import *  # noqa: F401,F403
from .calibration import *  # noqa: F401,F403
from .depth import *  # noqa: F401,F403
from .uncertainty import *  # noqa: F401,F403
from .fractionation import *  # noqa: F401,F403
from .reduction import *  # noqa: F401,F403
from .thermometry import *  # noqa: F401,F403
from .results import *  # noqa: F401,F403
from .pipeline import *  # noqa: F401,F403
from .reproducibility import *  # noqa: F401,F403
from .project import *  # noqa: F401,F403

from .constants import __all__ as _constants_all
from .standards import __all__ as _standards_all
from .io import __all__ as _io_all
from .visualization import __all__ as _visualization_all
from .mpl_concordia import __all__ as _mpl_concordia_all
from .concentration import __all__ as _concentration_all
from .ratios import __all__ as _ratios_all
from .isoplot import __all__ as _isoplot_all
from .calibration import __all__ as _calibration_all
from .depth import __all__ as _depth_all
from .uncertainty import __all__ as _uncertainty_all
from .fractionation import __all__ as _fractionation_all
from .reduction import __all__ as _reduction_all
from .thermometry import __all__ as _thermometry_all
from .results import __all__ as _results_all
from .pipeline import __all__ as _pipeline_all
from .reproducibility import __all__ as _reproducibility_all
from .project import __all__ as _project_all

__all__ = [
    *_constants_all,
    *_standards_all,
    *_io_all,
    *_visualization_all,
    *_mpl_concordia_all,
    *_concentration_all,
    *_ratios_all,
    *_isoplot_all,
    *_calibration_all,
    *_depth_all,
    *_uncertainty_all,
    *_fractionation_all,
    *_reduction_all,
    *_thermometry_all,
    *_results_all,
    *_pipeline_all,
    *_reproducibility_all,
    *_project_all,
]
