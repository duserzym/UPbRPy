"""Calibration models for fractionation and detector cross-calibration."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .qtegra import COUNTER_SATURATED, QtegraSession, recover_xcal


@dataclass
class ChannelCrossCalibration:
    """Detector factors and one-sigma fit errors for one channel.

    Drift arrays and acquisition labels share chronological order. The
    intercept describes the overlap fit; saturated Y uses only the factor.
    """

    channel: str
    qtegra_factor: float | None
    factor: float
    factor_se: float
    intercept: float
    intercept_se: float
    n_points: int
    n_acquisitions: int
    times_s: np.ndarray | None = None
    factors: np.ndarray | None = None
    factors_se: np.ndarray | None = None
    raw_factors: np.ndarray | None = None
    acquisition_labels: list[str] = field(default_factory=list)

    def factor_at(self, t_s) -> np.ndarray:
        """Evaluate a constant factor or drift with endpoint-holding interpolation."""
        times = np.asarray(t_s, dtype=float)
        if self.times_s is None or self.factors is None:
            return np.full_like(times, self.factor, dtype=float)
        return np.asarray(np.interp(times, self.times_s, self.factors), dtype=float)


@dataclass
class CrossCalibration:
    """Session detector calibration and the settings used to fit it."""

    mode: str
    on: str
    window: tuple[float, float]
    channels: dict[str, ChannelCrossCalibration]
    primary_standard: str | None = None
    smoothing_s: float = 1800.0
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        """Return JSON-safe fit settings, diagnostics and per-channel factors.

        Unknown or nonfinite numeric values are represented by None.
        """
        def number(value):
            return float(value) if value is not None and np.isfinite(value) else None

        channels = {}
        for name, fit in self.channels.items():
            record = {
                key: number(getattr(fit, key))
                for key in (
                    "qtegra_factor", "factor", "factor_se", "intercept", "intercept_se",
                )
            }
            record.update({
                "n_points": int(fit.n_points),
                "n_acquisitions": int(fit.n_acquisitions),
                "relative_difference_from_qtegra": (
                    number(fit.factor / fit.qtegra_factor - 1)
                    if fit.qtegra_factor is not None and fit.qtegra_factor != 0 else None
                ),
                "acquisition_labels": [str(label) for label in fit.acquisition_labels],
            })
            if self.mode == "drift":
                for key in ("times_s", "factors", "factors_se", "raw_factors"):
                    values = getattr(fit, key)
                    record[key] = None if values is None else [number(v) for v in values]
            channels[str(name)] = record
        return {
            "mode": str(self.mode),
            "on": str(self.on),
            "window": [number(v) for v in self.window],
            "primary_standard": (
                None if self.primary_standard is None else str(self.primary_standard)
            ),
            "smoothing_s": number(self.smoothing_s),
            "warnings": [str(warning) for warning in self.warnings],
            "channels": channels,
        }


def _require_analog_counter(session):
    if not session.has("analog_counter"):
        raise ValueError(
            f"Qtegra format {session.format!r} lacks the analog_counter capability "
            "required for detector cross-calibration."
        )


def _session_start_offsets(session):
    """Return start seconds from the earliest recorded start."""
    starts = [a.start for a in session.acquisitions if a.start is not None]
    origin = min(starts) if starts else None
    offsets = []
    for acquisition in session.acquisitions:
        if acquisition.start is not None:
            offsets.append((acquisition.start - origin).total_seconds())
        else:
            raise ValueError(f"{acquisition.label}: no acquisition start.")
    return offsets


def _channel_session_time(acquisition, index, offset):
    if acquisition.time is None:
        raise ValueError(f"{acquisition.label}: no sweep times.")
    times = acquisition.time[:, index] if acquisition.time_per_channel else acquisition.time
    return offset + times


def _inverse_variance_weights(errors, factors):
    # A precision-scaled floor handles exact synthetic fits without infinite
    # weights. Scaling all inverse variances by their maximum avoids overflow.
    floor = np.finfo(float).eps * max(1.0, float(np.max(np.abs(factors))))
    guarded = np.maximum(errors, floor)
    return (np.min(guarded) / guarded) ** 2


def fit_cross_calibration(session, channel=None, window=(1e5, 2.4e6),
                          on="all", mode="constant", *, primary_standard=None,
                          sep="_", smoothing_s=1800.0) -> CrossCalibration:
    """Fit counter on analog in the overlap window, optionally with time drift.

    Constant fits use OLS with an intercept and RSS/(n-2) covariance. Drift
    slopes hold that intercept fixed and use RSS/(n-1) for each acquisition.
    Gaussian smoothing uses inverse slope variances. Channels with fewer than
    three points are omitted with a warning, or raise when explicitly requested.
    Qtegra mode records exported/recovered factors without fitting.
    """
    if mode not in {"qtegra", "constant", "drift"}:
        raise ValueError("mode must be 'qtegra', 'constant', or 'drift'.")
    if on not in {"all", "primary_standard"}:
        raise ValueError("on must be 'all' or 'primary_standard'.")
    if on == "primary_standard" and not primary_standard:
        raise ValueError("primary_standard is required for on='primary_standard'.")
    window = tuple(float(value) for value in window)
    if len(window) != 2 or not np.isfinite(window).all() or window[0] > window[1]:
        raise ValueError("window must contain two finite, increasing bounds.")
    smoothing_s = float(smoothing_s)
    if not np.isfinite(smoothing_s) or smoothing_s <= 0:
        raise ValueError("smoothing_s must be finite and positive.")
    if mode != "qtegra":
        _require_analog_counter(session)
    names = list(session.channel_names) if channel is None else (
        [channel] if isinstance(channel, str) else list(channel)
    )
    unknown = [name for name in names if name not in session.channel_names]
    if unknown:
        raise ValueError(f"Unknown detector channel(s): {', '.join(unknown)}")
    result = CrossCalibration(mode, on, window, {}, primary_standard, smoothing_s)
    qtegra_factors = recover_xcal(session)
    for acquisition in session.acquisitions:
        for metadata in acquisition.channels:
            if metadata.xcal is not None:
                qtegra_factors[metadata.name] = float(metadata.xcal)

    offsets = _session_start_offsets(session) if mode == "drift" else None
    for name in dict.fromkeys(names):
        qtegra_factor = qtegra_factors.get(name)
        if mode == "qtegra":
            if qtegra_factor is not None:
                result.channels[name] = ChannelCrossCalibration(
                    name, qtegra_factor, qtegra_factor, np.nan, 0.0, np.nan, 0, 0,
                )
            continue
        points = []
        for position, acquisition in enumerate(session.acquisitions):
            if (on == "primary_standard"
                    and acquisition.label.split(sep)[0].casefold()
                    != str(primary_standard).casefold()):
                continue
            if acquisition.analog is None or acquisition.counter is None:
                continue
            if name not in acquisition.channel_names:
                continue
            index = acquisition.channel_index(name)
            analog = acquisition.analog[:, index]
            counter = acquisition.counter[:, index]
            mask = (np.isfinite(counter) & (counter != COUNTER_SATURATED)
                    & (counter >= window[0]) & (counter <= window[1])
                    & np.isfinite(analog) & (analog > 0))
            if mask.any():
                points.append((position, acquisition, index, mask, analog[mask], counter[mask]))
        n = sum(len(point[4]) for point in points)
        problem = None
        if n < 3:
            problem = f"{name}: fewer than 3 points in the counter overlap window ({n})."
        else:
            analog = np.concatenate([point[4] for point in points])
            counter = np.concatenate([point[5] for point in points])
            design = np.column_stack((analog, np.ones(n)))
            coefficients, _, rank, _ = np.linalg.lstsq(design, counter, rcond=None)
            if rank < 2:
                problem = f"{name}: analog values do not span a full-rank overlap fit."
        if problem:
            if channel is not None:
                raise ValueError(problem)
            result.warnings.append(problem)
            continue
        factor, intercept = coefficients
        residuals = counter - design @ coefficients
        covariance = (residuals @ residuals / (n - 2)) * np.linalg.inv(design.T @ design)
        factor_se, intercept_se = np.sqrt(np.diag(covariance))
        fit = ChannelCrossCalibration(
            name, qtegra_factor, float(factor), float(factor_se),
            float(intercept), float(intercept_se), n, len(points),
            acquisition_labels=[point[1].label for point in points],
        )
        result.channels[name] = fit
        if mode == "constant":
            continue
        raw_factors, errors, mid_times, labels = [], [], [], []
        for position, acquisition, index, mask, analog, counter in points:
            if len(analog) < 5:
                continue
            sum_squares = analog @ analog
            slope = (analog @ (counter - intercept)) / sum_squares
            residuals = counter - intercept - slope * analog
            error = np.sqrt((residuals @ residuals) / (len(analog) - 1) / sum_squares)
            times = _channel_session_time(acquisition, index, offsets[position])[mask]
            if not (np.isfinite(slope) and np.isfinite(error) and np.isfinite(times).all()):
                result.warnings.append(f"{name}: skipped nonfinite drift fit for {acquisition.label}.")
                continue
            raw_factors.append(slope)
            errors.append(error)
            mid_times.append(float(np.mean(times)))
            labels.append(acquisition.label)
        if not raw_factors:
            result.warnings.append(
                f"{name}: no acquisition qualifies for drift; using the constant fit."
            )
            continue
        order = np.argsort(mid_times, kind="stable")
        fit.times_s = np.asarray(mid_times)[order]
        fit.raw_factors = np.asarray(raw_factors)[order]
        errors = np.asarray(errors)[order]
        fit.acquisition_labels = [labels[i] for i in order]
        fit.n_acquisitions = len(order)
        weights = _inverse_variance_weights(errors, fit.raw_factors)
        normalized = weights / weights.sum()
        fit.factor = float(normalized @ fit.raw_factors)
        fit.factor_se = float(np.sqrt(np.sum((normalized * errors) ** 2)))
        kernel = np.exp(-0.5 * ((fit.times_s[:, None] - fit.times_s) / smoothing_s) ** 2)
        smoothed_weights = kernel * weights
        smoothed_weights /= smoothed_weights.sum(axis=1, keepdims=True)
        fit.factors = smoothed_weights @ fit.raw_factors
        fit.factors_se = np.sqrt(np.sum((smoothed_weights * errors) ** 2, axis=1))
    return result


def apply_cross_calibration(session, calibration) -> QtegraSession:
    """Deep-copy a session and replace only calibrated, saturated finite-analog Y.

    Qtegra mode returns an unchanged copy. Other modes append one warning
    naming the calibrated channels and mode; all source measurements remain.
    """
    result = deepcopy(session)
    if calibration.mode == "qtegra":
        return result
    _require_analog_counter(session)
    offsets = _session_start_offsets(session) if calibration.mode == "drift" else None
    for position, acquisition in enumerate(result.acquisitions):
        if acquisition.analog is None or acquisition.counter is None:
            continue
        for name, fit in calibration.channels.items():
            if name not in acquisition.channel_names:
                continue
            index = acquisition.channel_index(name)
            analog = acquisition.analog[:, index]
            mask = (acquisition.counter[:, index] == COUNTER_SATURATED) & np.isfinite(analog)
            if not mask.any():
                continue
            times = (
                _channel_session_time(acquisition, index, offsets[position])[mask]
                if offsets is not None else np.zeros(mask.sum())
            )
            acquisition.y[mask, index] = analog[mask] * fit.factor_at(times)
    result.warnings.append(
        f"Detector cross-calibration recalibrated channels "
        f"{', '.join(calibration.channels) or '(none)'} with mode {calibration.mode}."
    )
    return result


ArrayFunction = Callable[[np.ndarray], np.ndarray]


@dataclass
class CalibrationModel:
    """A fitted one-dimensional correction model.

    Instances are callable and retain lightweight fit diagnostics. They do not
    calculate or store coefficient or prediction covariance.
    """

    method: str
    predictor: ArrayFunction
    parameters: np.ndarray
    x: np.ndarray
    y: np.ndarray
    residuals: np.ndarray
    residual_standard_deviation: float
    degrees_of_freedom: int
    settings: dict

    def __call__(self, values):
        """Evaluate the fitted correction model at one or more coordinates."""
        values = np.asarray(values, dtype=float)
        return np.asarray(self.predictor(values), dtype=float)

    @property
    def x_range(self):
        """Return the minimum and maximum fitted calibration coordinates."""
        return float(np.nanmin(self.x)), float(np.nanmax(self.x))


def _prepare_xy(x, y, weights=None):
    """Validate, filter, and sort paired one-dimensional calibration observations."""
    x = np.asarray(x, dtype=float)
    y = np.asarray(y, dtype=float)
    if x.shape != y.shape:
        raise ValueError("x and y must have the same shape.")
    finite = np.isfinite(x) & np.isfinite(y)
    if weights is not None:
        weights = np.asarray(weights, dtype=float)
        if weights.shape != x.shape:
            raise ValueError("weights must match x and y.")
        finite &= np.isfinite(weights) & (weights > 0)
        weights = weights[finite]
    x = x[finite]
    y = y[finite]
    if x.size < 1:
        raise ValueError("At least one finite calibration observation is required.")
    order = np.argsort(x)
    x = x[order]
    y = y[order]
    if weights is not None:
        weights = weights[order]
    return x, y, weights


def _exponential(time, asymptote, amplitude, rate):
    """Evaluate an asymptotic exponential-decay curve at calibration coordinates."""
    return asymptote + amplitude * np.exp(-rate * time)


def running_mean_curve(values, window=5):
    """Return an edge-padded odd-window running mean.

    The output retains the input length, which permits direct interpolation
    back onto arbitrary downhole or session coordinates.
    """
    values = np.asarray(values, dtype=float)
    if values.ndim != 1:
        raise ValueError("values must be one-dimensional")
    if values.size == 0:
        return values.copy()
    window = max(int(window), 1)
    if window % 2 == 0:
        window += 1
    maximum = values.size if values.size % 2 else values.size - 1
    window = min(window, max(maximum, 1))
    if window <= 1:
        return values.copy()
    padding = window // 2
    padded = np.pad(values, (padding, padding), mode="edge")
    kernel = np.full(window, 1.0 / window)
    return np.convolve(padded, kernel, mode="valid")


def fit_calibration_model(
    x,
    y,
    method="polynomial",
    *,
    weights=None,
    degree=2,
    smoothing=None,
    running_mean_window=5,
    exponential_initial_decay=None,
    p0=None,
    bounds=(-np.inf, np.inf),
):
    """Fit a built-in or user-supplied one-dimensional calibration function.

    Parameters
    ----------
    x, y : array-like
        Calibration coordinates and correction factors.
    method : str or callable
        Built-ins are ``'interpolate'``, ``'mean'``, ``'linear'``,
        ``'polynomial'``, ``'orthogonal_polynomial'``, ``'exponential'``,
        ``'spline'``, and ``'running_mean'``. A callable is treated as
        ``f(x, *parameters)`` and fitted with :func:`scipy.optimize.curve_fit`;
        a one-argument callable is used directly as an already-calibrated
        predictor.
    weights : array-like, optional
        Relative least-squares weights. These affect fitting only; UPbRPy does
        not propagate coefficient covariance.
    degree : int, optional
        Polynomial degree for regular or orthogonal polynomial models.
    smoothing : float, optional
        Smoothing factor passed to ``UnivariateSpline``. ``None`` uses SciPy's
        default.
    running_mean_window : int, optional
        Odd smoothing window used by the running-mean model.
    exponential_initial_decay : float, optional
        Positive starting decay rate for exponential optimization. When
        omitted, the reciprocal calibration-span heuristic is used.
    p0, bounds : optional
        Initial parameters and bounds for exponential or custom parametric
        functions.
    """
    # Calibration fitting is not needed to construct the desktop shell.
    # Delay SciPy's compiled interpolation/optimization modules until the
    # first correction model is actually fitted in a worker.
    from scipy.interpolate import UnivariateSpline, interp1d
    from scipy.optimize import curve_fit

    x, y, weights = _prepare_xy(x, y, weights)
    settings = {
        "degree": int(degree),
        "smoothing": smoothing,
        "running_mean_window": int(running_mean_window),
        "exponential_initial_decay": exponential_initial_decay,
    }

    if callable(method):
        method_name = getattr(method, "__name__", "custom")
        if p0 is None:
            try:
                direct = np.asarray(method(x), dtype=float)
            except TypeError:
                direct = None
            if direct is not None and direct.shape == y.shape:
                predictor = method
                parameters = np.array([], dtype=float)
                parameter_count = 0
            else:
                sigma = None if weights is None else 1 / weights
                parameters, _ = curve_fit(
                    method, x, y, p0=p0, bounds=bounds, sigma=sigma,
                    absolute_sigma=False, maxfev=100000,
                )
                predictor = lambda values, f=method, p=parameters: f(values, *p)
                parameter_count = len(parameters)
        else:
            sigma = None if weights is None else 1 / weights
            parameters, _ = curve_fit(
                method, x, y, p0=p0, bounds=bounds, sigma=sigma,
                absolute_sigma=False, maxfev=100000,
            )
            predictor = lambda values, f=method, p=parameters: f(values, *p)
            parameter_count = len(parameters)
    else:
        method_name = str(method).strip().lower().replace("-", "_").replace(" ", "_")
        aliases = {
            "average": "mean",
            "constant": "mean",
            "linear_interpolation": "interpolate",
            "interpolated": "interpolate",
            "pointwise_mean": "interpolate",
            "poly": "polynomial",
            "orthogonal": "orthogonal_polynomial",
            "cubic_spline": "spline",
            "exp": "exponential",
        }
        method_name = aliases.get(method_name, method_name)

        if method_name == "mean":
            parameters = np.array([np.average(y, weights=weights)], dtype=float)
            predictor = lambda values, value=parameters[0]: np.full_like(
                np.asarray(values, dtype=float), value, dtype=float
            )
            parameter_count = 1
        elif method_name == "interpolate":
            if x.size < 2:
                raise ValueError("Interpolation requires at least two observations.")
            interpolator = interp1d(x, y, kind="linear", fill_value="extrapolate")
            predictor = interpolator
            parameters = y.copy()
            parameter_count = len(y)
        elif method_name in {"linear", "polynomial"}:
            fit_degree = 1 if method_name == "linear" else int(degree)
            if fit_degree < 0 or x.size <= fit_degree:
                raise ValueError("Polynomial degree must be nonnegative and smaller than the observation count.")
            parameters = np.polyfit(x, y, fit_degree, w=weights)
            predictor = lambda values, p=parameters: np.polyval(p, values)
            parameter_count = len(parameters)
            settings["degree"] = fit_degree
        elif method_name == "orthogonal_polynomial":
            fit_degree = int(degree)
            if fit_degree < 0 or x.size <= fit_degree:
                raise ValueError("Polynomial degree must be nonnegative and smaller than the observation count.")
            orthogonal_fit = np.polynomial.Legendre.fit(x, y, fit_degree, w=weights)
            parameters = orthogonal_fit.coef.copy()
            predictor = orthogonal_fit
            parameter_count = len(parameters)
            settings["degree"] = fit_degree
            settings["domain"] = orthogonal_fit.domain.tolist()
        elif method_name == "exponential":
            x_origin = float(x[0])
            shifted_x = x - x_origin
            if p0 is None:
                span = max(float(np.ptp(shifted_x)), np.finfo(float).eps)
                initial_decay = (
                    1 / span
                    if exponential_initial_decay is None
                    else float(exponential_initial_decay)
                )
                if not np.isfinite(initial_decay) or initial_decay <= 0:
                    raise ValueError(
                        "exponential_initial_decay must be positive"
                    )
                p0 = (
                    float(y[-1]),
                    float(y[0] - y[-1]),
                    initial_decay,
                )
            if bounds == (-np.inf, np.inf):
                bounds = ((-np.inf, -np.inf, 0.0), (np.inf, np.inf, np.inf))
            sigma = None if weights is None else 1 / weights
            parameters, _ = curve_fit(
                _exponential, shifted_x, y, p0=p0, bounds=bounds, sigma=sigma,
                absolute_sigma=False, maxfev=100000,
            )
            predictor = lambda values, p=parameters, origin=x_origin: _exponential(
                np.asarray(values, dtype=float) - origin, *p
            )
            parameter_count = len(parameters)
            settings["x_origin"] = x_origin
        elif method_name == "spline":
            if x.size < 2:
                raise ValueError("Spline fitting requires at least two observations.")
            spline = UnivariateSpline(
                x, y, w=weights, k=min(3, x.size - 1), s=smoothing
            )
            predictor = spline
            parameters = spline.get_coeffs().copy()
            parameter_count = len(parameters)
        elif method_name == "running_mean":
            if x.size < 2:
                raise ValueError(
                    "Running-mean fitting requires at least two observations."
                )
            smoothed = running_mean_curve(y, running_mean_window)
            interpolator = interp1d(
                x,
                smoothed,
                kind="linear",
                fill_value="extrapolate",
            )
            predictor = interpolator
            parameters = smoothed
            parameter_count = 1
            settings["running_mean_window"] = min(
                int(running_mean_window),
                len(smoothed),
            )
        else:
            raise ValueError(
                "Unknown calibration method. Choose mean, interpolate, linear, "
                "polynomial, orthogonal_polynomial, exponential, spline, "
                "running_mean, or pass a callable."
            )

    residuals = y - np.asarray(predictor(x), dtype=float)
    degrees_of_freedom = max(x.size - parameter_count, 1)
    residual_standard_deviation = float(
        np.sqrt(np.sum(residuals**2) / degrees_of_freedom)
    )
    return CalibrationModel(
        method=method_name,
        predictor=predictor,
        parameters=np.asarray(parameters, dtype=float),
        x=x,
        y=y,
        residuals=residuals,
        residual_standard_deviation=residual_standard_deviation,
        degrees_of_freedom=degrees_of_freedom,
        settings=settings,
    )


def fit_multiplicative_calibration_model(
    x,
    factors,
    method="polynomial",
    *,
    fit_space="linear",
    weights=None,
    **model_kwargs,
):
    """Fit positive multiplicative corrections in linear or log space.

    The ``logratio`` mode implements the additive fractionation relationship
    described by McLean et al. (2016),
    ``log(R_true) = log(C) + log(R_measured)``.  Built-in calibration models
    are fitted to ``log(C)`` and exponentiated when evaluated, ensuring every
    returned correction factor remains positive.  Residual diagnostics are
    returned in ordinary factor units for compatibility with existing plots
    and uncertainty columns.

    Custom callables retain their established factor-space semantics and are
    therefore rejected in log space.  A custom log-space model can still be
    supplied after fitting by evaluating it outside this convenience wrapper.
    """
    normalized_space = str(fit_space).strip().lower().replace("-", "_")
    aliases = {
        "linear": "linear",
        "arithmetic": "linear",
        "ratio": "linear",
        "log": "logratio",
        "log_ratio": "logratio",
        "logratio": "logratio",
        "geometric": "logratio",
    }
    try:
        normalized_space = aliases[normalized_space]
    except KeyError as error:
        raise ValueError("fit_space must be 'linear' or 'logratio'.") from error
    if normalized_space == "linear":
        return fit_calibration_model(
            x,
            factors,
            method=method,
            weights=weights,
            **model_kwargs,
        )
    if callable(method):
        raise ValueError(
            "Log-space calibration currently requires a built-in model name; "
            "custom callables retain factor-space semantics."
        )

    x_array = np.asarray(x, dtype=float)
    factor_array = np.asarray(factors, dtype=float)
    if x_array.shape != factor_array.shape:
        raise ValueError("x and factors must have the same shape.")
    valid = np.isfinite(x_array) & np.isfinite(factor_array) & (factor_array > 0)
    if weights is not None:
        weight_array = np.asarray(weights, dtype=float)
        if weight_array.shape != factor_array.shape:
            raise ValueError("weights must match x and factors.")
        valid &= np.isfinite(weight_array) & (weight_array > 0)
        # If supplied weights are inverse absolute factor uncertainties, the
        # delta method gives inverse log-factor uncertainty C / sigma_C.
        log_weights = factor_array[valid] * weight_array[valid]
    else:
        log_weights = None
    if not np.any(valid):
        raise ValueError("Log-space calibration requires positive factors.")

    log_model = fit_calibration_model(
        x_array[valid],
        np.log(factor_array[valid]),
        method=method,
        weights=log_weights,
        **model_kwargs,
    )
    predictor = lambda values, fitted=log_model: np.exp(fitted(values))
    fitted_factors = predictor(x_array[valid])
    factor_residuals = factor_array[valid] - fitted_factors
    residual_standard_deviation = float(
        np.sqrt(
            np.sum(factor_residuals**2)
            / max(log_model.degrees_of_freedom, 1)
        )
    )
    settings = dict(log_model.settings)
    settings["fit_space"] = "logratio"
    settings["log_residual_standard_deviation"] = (
        log_model.residual_standard_deviation
    )
    return CalibrationModel(
        method=log_model.method,
        predictor=predictor,
        parameters=log_model.parameters,
        x=x_array[valid],
        y=factor_array[valid],
        residuals=factor_residuals,
        residual_standard_deviation=residual_standard_deviation,
        degrees_of_freedom=log_model.degrees_of_freedom,
        settings=settings,
    )


def evaluate_correction_result(result, values):
    """Evaluate either a modern or legacy correction-result mapping.

    Modern mappings expose ``predictor``; older downhole results used
    ``interpolation`` or polynomial coefficients stored in ``fit``.
    """
    if "predictor" in result:
        return np.asarray(result["predictor"](values), dtype=float)
    if "interpolation" in result:
        return np.asarray(result["interpolation"](values), dtype=float)
    return np.asarray(np.polyval(result["fit"], values), dtype=float)


__all__ = [
    "CrossCalibration",
    "ChannelCrossCalibration",
    "fit_cross_calibration",
    "apply_cross_calibration",
    "CalibrationModel",
    "fit_calibration_model",
    "fit_multiplicative_calibration_model",
    "evaluate_correction_result",
    "running_mean_curve",
]
