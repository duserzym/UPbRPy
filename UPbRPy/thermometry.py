"""Mineral thermometry calculations and uncertainty envelopes."""

from __future__ import annotations

import numpy as np

def Zr_in_rutile_thermometry(Zr_ppm, pressure, a_SiO2=1, method='Kohn2020'):
    '''
    function for calculating Zr in rutile thermometer after Kohn (2020) or Ferry and Watson (2007)

    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in rutile in ppm
    pressure : float
        pressure in GPa
    a_SiO2 : float
        activity of SiO2
    method : str
        method for calculation, either 'Kohn2020' or 'FW2007'

    Returns
    -------
    float
        temperature in degree Celsius
    '''
    R = 8.314
    if method == 'Kohn2020':
        pressure = pressure * 1e4 # convert to bar
        T_K = (71360 + 0.378 * pressure - 0.130 * Zr_ppm) / (130.66 - R*np.log(Zr_ppm)) 
    elif method == 'FW2007':
        T_K = 4530 / (7.420 - np.log10(a_SiO2) - np.log10(Zr_ppm))
    return np.round(T_K - 273.15, 2)

def Zr_in_rutile_thermometry_uncertainty(Zr_ppm, pressure_list, a_SiO2_list, method='Kohn2020'):
    '''
    function for calculating Zr in rutile thermometer uncertainty after Kohn (2020) or Ferry and Watson (2007)

    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in rutile in ppm
    pressure_list : list
        list of pressure in GPa
    a_SiO2_list : list
        list of activity of SiO2
    method : str
        method for calculation, either 'Kohn2020' or 'FW2007'

    Returns
    -------
    upper : float
        the 97.5 percentile temperature in degree Celsius
    lower : float
        the 2.5 percentile temperature in degree Celsius
    '''
    T_list = []
    for pressure, a_SiO2 in zip(pressure_list, a_SiO2_list):
        T_list.append(Zr_in_rutile_thermometry(Zr_ppm, pressure, a_SiO2, method))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

def Ti_in_zircon_thermometry(Ti_ppm, a_SiO2, a_TiO2):
    '''
    function for calculating Ti in zircon thermometer after Ferry and Watson (2007)
    
    Parameters
    ----------
    Ti_ppm : float
        Ti concentration in zircon in ppm
    a_SiO2 : float
        activity of SiO2
    a_TiO2 : float
        activity of TiO2

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_K = 4800 / (5.711 - np.log10(a_SiO2) + np.log10(a_TiO2) - np.log10(Ti_ppm))

    return np.round(T_K - 273.15, 2)

def Ti_in_zircon_thermometry_uncertainty(Ti_ppm, a_SiO2_list, a_TiO2_list):
    '''
    function for calculating Ti in zircon thermometer uncertainty
    
    Parameters
    ----------
    Ti_ppm : float
        Ti concentration in zircon in ppm
    a_SiO2_list : list
        list of activity of SiO2
    a_TiO2_list : list
        list of activity of TiO2

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_list = []
    for a_SiO2, a_TiO2 in zip(a_SiO2_list, a_TiO2_list):
        T_list.append(Ti_in_zircon_thermometry(Ti_ppm, a_SiO2, a_TiO2))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

def Zr_in_titanite_thermometry(Zr_ppm, a_SiO2, a_TiO2, pressure):
    '''
    function for calculating Zr in titanite thermometer after Hayden et al., 2007
    
    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in titanite in ppm
    a_SiO2 : float
        activity of SiO2
    a_TiO2 : float
        activity of TiO2
    pressure : float
        pressure in GPa

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_K = (7708 + 960 * pressure) / (10.52 - np.log10(Zr_ppm) - np.log10(a_TiO2) - np.log10(a_SiO2))

    return round(T_K - 273.15, 2)

def Zr_in_titanite_thermometry_uncertainty(Zr_ppm, a_SiO2_list, a_TiO2_list, pressure_list):
    '''
    function for calculating Zr in titanite thermometer uncertainty 
    
    Parameters
    ----------
    Zr_ppm : float
        Zr concentration in titanite in ppm
    a_SiO2_list : list
        list of activity of SiO2
    a_TiO2_list : list
        list of activity of TiO2
    pressure : float
        pressure in GPa

    Returns
    -------
    float
        temperature in degree Celsius

    '''

    T_list = []
    for a_SiO2, a_TiO2, pressure in zip(a_SiO2_list, a_TiO2_list, pressure_list):
        T_list.append(Zr_in_titanite_thermometry(Zr_ppm, a_SiO2, a_TiO2, pressure))
    upper, lower = np.percentile(T_list, [97.5, 2.5])
    return upper, lower

__all__ = [
    'Zr_in_rutile_thermometry',
    'Zr_in_rutile_thermometry_uncertainty',
    'Ti_in_zircon_thermometry',
    'Ti_in_zircon_thermometry_uncertainty',
    'Zr_in_titanite_thermometry',
    'Zr_in_titanite_thermometry_uncertainty',
]
