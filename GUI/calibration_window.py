from PyQt5.QtWidgets import QMainWindow, QWidget, QApplication 
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QWidget, QPushButton, QComboBox, QSpinBox, QLabel, QFileDialog

import pandas as pd
pd.options.mode.chained_assignment = None

import warnings
warnings.filterwarnings("ignore", message="The default dtype for empty Series will be 'object' instead of 'float64' in a future version.")


import numpy as np
import sys
import plotly.graph_objects as go

from custom_widgets import *
from util import *



class CalibrationWindow(QMainWindow):
    def __init__(self, experiment, total_data, channels, primary_UPb_standard, primary_concentration_standard, reference_materials_df):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS fractionation Calibration Window")
        self.setGeometry(100, 100, 1800, 800)
        self.experiment = experiment
        self.total_data = total_data
        self.channels = channels
        self.primary_UPb_standard = primary_UPb_standard
        self.primary_concentration_standard = primary_concentration_standard

        self.concentration_RM = reference_materials_df[reference_materials_df['type'] == 'glass']
        self.UPb_RM = reference_materials_df[reference_materials_df['type'] != 'glass']

        # in this window we only work with baseline-corrected data so let's do baseline correction
        # first make a copy of the total data
        self.baseline_corrected_data = pd.DataFrame()

        for spot_name in self.total_data['spot_name'].unique():
            this_spot_data = self.total_data[self.total_data['spot_name'] == spot_name]
            # get the baseline start and end times for this spot
            baseline_start_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_start_times'].values[0]
            baseline_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]
            this_spot_baseline = this_spot_data[(this_spot_data['Time'] >= baseline_start_time) & (this_spot_data['Time'] < baseline_end_time)]
            for channel in self.channels:
                this_spot_data.loc[:, channel] = this_spot_data[channel] - this_spot_baseline[channel].mean()
            self.baseline_corrected_data = pd.concat([self.baseline_corrected_data, this_spot_data])
            # .loc[this_spot_data.index, self.channels] = this_spot_data[self.channels]
        # print(self.baseline_corrected_data)
        self.baseline_corrected_data.sort_values(by='Time', inplace=True, ignore_index=True)
        
        self.initUI()

    def initUI(self):
        # print(self.experiment)
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)

        # initialize the calibration dataframe to keep track of the calibration results
        self.calibration_df = pd.DataFrame()
        # set default fits to the session wide calibration to polynomial fits with degree 3
        self.calibration_df.index = ['downhole 206Pb/238U', 'downhole 207Pb/235U', 'downhole 207Pb/206Pb', 'downhole 208Pb/232Th', 
                                     'session 206Pb/238U', 'session 207Pb/235U', 'session 207Pb/206Pb', 'session 208Pb/232Th']
        self.calibration_df['Fit type'] = ['Exponential', 'Exponential', 'Exponential', 'Exponential', 
                                           'Polynomial', 'Polynomial', 'Polynomial', 'Polynomial']
        self.calibration_df['Fit equation'] = None
        self.calibration_df['Polynomial degree'] = [0,0,0,0,
                                                    3,3,3,3]
        self.calibration_df['Moving average window'] = None
        self.calibration_df['Fit parameters'] = None
        self.calibration_df['Fit parameters'] = self.calibration_df['Fit parameters'].astype(object)
        self.calibration_df['Quality'] = None

        self.fractionation_dropdown_label = QLabel('Select a ratio to Calibrate')
        self.fractionation_dropdown = QComboBox()
        self.fractionation_dropdown.addItem('downhole 206Pb/238U')
        self.fractionation_dropdown.addItem('downhole 207Pb/235U')
        self.fractionation_dropdown.addItem('downhole 207Pb/206Pb')
        self.fractionation_dropdown.addItem('downhole 208Pb/232Th')
        self.fractionation_dropdown.addItem('session 206Pb/238U')
        self.fractionation_dropdown.addItem('session 207Pb/235U')
        self.fractionation_dropdown.addItem('session 207Pb/206Pb')
        self.fractionation_dropdown.addItem('session 208Pb/232Th')

        self.fit_type_dropdown_label = QLabel('Select a fit type')
        self.fit_type_dropdown = QComboBox()
        self.fit_type_dropdown.addItem('Linear')
        self.fit_type_dropdown.addItem('Exponential')
        self.fit_type_dropdown.addItem('Polynomial')
        self.fit_type_dropdown.addItem('Moving Average')

        # add a text box to show the type of fit with example equation
        self.fit_equation_label = QLabel('Fit Equation')
        self.fit_equation_text = QLabel('y = bx + a')

        self.poly_degree_label = QLabel('Polynomial fit degree')
        self.poly_degree_setting = QSpinBox()
        self.poly_degree_setting.setMinimum(1)
        self.poly_degree_setting.setMaximum(10)
        self.poly_degree_setting.setValue(1)

        self.moving_average_window_label = QLabel('Moving average window (s)')
        self.moving_average_window_setting = QSpinBox()
        self.moving_average_window_setting.setMinimum(1)

        self.quality_label = QLabel('Standard error of fit')
        self.quality_label_text = QLabel('R squared value of fit: 0.0')
        
        # first plot on top shows the aligned downhole fractionation of 6/8 ratios
        self.DH_68_fractionation_plot = PlotCanvas()
        self.DH_68_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{206}Pb/^{238}U\ \text{ratio}$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{206}Pb/^{238}U\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # second plot shows the aligned downhole fractionation of 7/5 ratios
        self.DH_75_fractionation_plot = PlotCanvas()
        self.DH_75_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{207}Pb/^{235}U\ \text{ratio}$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{207}Pb/^{235}U\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # third plot shows the aligned downhole fractionation of 7/6 ratios
        self.DH_76_fractionation_plot = PlotCanvas()
        self.DH_76_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{207}Pb/^{206}Pb\ \text{ratio}$',
                                                                'x': 0.5,
                                                                'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{207}Pb/^{206}Pb\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis',
                                                                     tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # fourth plot shows the aligned downhole fractionation of 208Pb 232Th ratios
        self.DH_82_fractionation_plot = PlotCanvas()
        self.DH_82_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{208}Pb/^{232}Th\ \text{ratio}$',
                                                                'x': 0.5,
                                                                'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{208}Pb/^{232}Th\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis',
                                                                     tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # first plot on bottom shows the session wide primary standard fractionation of 6/8 ratios
        self.session_68_fractionation_plot = PlotCanvas()
        self.session_68_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{206}Pb/^{238}U\ \text{Fractionation}$', 
                                                                    'x': 0.5, 
                                                                    'xanchor': 'center'}, 
                                                            yaxis=dict(title=r'$^{206}Pb/^{238}U$ ratio', title_font=dict(size=14)), 
                                                            xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                            legend=dict(x=0.5, xanchor='center', y=0, yanchor='bottom'))

        # second plot shows the session wide primary standard fractionation of 7/5 ratios
        self.session_75_fractionation_plot = PlotCanvas()
        self.session_75_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{207}Pb/^{235}U\ \text{Fractionation}$', 
                                                            'x': 0.5, 
                                                            'xanchor': 'center'}, 
                                                    yaxis=dict(title=r'$^{207}Pb/^{235}U$ Ratio', title_font=dict(size=14)), 
                                                    xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                    legend=dict(x=0.5, xanchor='center', y=0, yanchor='bottom'))
        
        # third plot shows the session wide primary standard fractionation of 6/8 ratios
        self.session_76_fractionation_plot = PlotCanvas()
        self.session_76_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{207}Pb/^{206}Pb\ \text{Fractionation}$',
                                                                    'x': 0.5,
                                                                    'xanchor': 'center'},
                                                            yaxis=dict(title=r'$^{207}Pb/^{206}Pb$ Ratio', title_font=dict(size=14)),
                                                            xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                            legend=dict(x=0.5, xanchor='center', y=0, yanchor='bottom'))
        
        # fourth pllto shows the session wide primary standard fractionation of 208Pb 232Th ratios
        self.session_82_fractionation_plot = PlotCanvas()
        self.session_82_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{208}Pb/^{232}Th\ \text{Fractionation}$',
                                                            'x': 0.5, 
                                                            'xanchor': 'center'}, 
                                                    yaxis=dict(title=r'$^{208}Pb/^{232}Th$ Ratio', title_font=dict(size=14)), 
                                                    xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                    legend=dict(x=0.5, xanchor='center', y=0, yanchor='bottom'))
        
        
        self.calibration_df['Plot'] = [self.DH_68_fractionation_plot,
                                        self.DH_75_fractionation_plot,
                                        self.DH_76_fractionation_plot,
                                        self.DH_82_fractionation_plot,
                                        self.session_68_fractionation_plot, 
                                       self.session_75_fractionation_plot, 
                                       self.session_76_fractionation_plot, 
                                       self.session_82_fractionation_plot]
        
        # add ok and cancel button to save or cancel the calibration
        self.apply_button = QPushButton("Apply")
        self.export_button = QPushButton("Export data")

        self.export_button.clicked.connect(self.on_click_export_button)

        self.layout = QHBoxLayout()
        self.main_widget.setLayout(self.layout)

        self.manual_calibration_layout = QVBoxLayout()
        self.all_plots_layout = QVBoxLayout()
        self.session_layout = QHBoxLayout()
        self.DH_layout = QHBoxLayout()
        # self.button_layout = QVBoxLayout()

        self.manual_calibration_layout.addWidget(self.fractionation_dropdown_label)
        self.manual_calibration_layout.addWidget(self.fractionation_dropdown)
        self.manual_calibration_layout.addWidget(self.fit_type_dropdown_label)
        self.manual_calibration_layout.addWidget(self.fit_type_dropdown)
        self.manual_calibration_layout.addWidget(self.fit_equation_label)
        self.manual_calibration_layout.addWidget(self.fit_equation_text)
        self.manual_calibration_layout.addWidget(self.poly_degree_label)
        self.manual_calibration_layout.addWidget(self.poly_degree_setting)
        self.manual_calibration_layout.addWidget(self.moving_average_window_label)
        self.manual_calibration_layout.addWidget(self.moving_average_window_setting)
        self.manual_calibration_layout.addWidget(self.quality_label)

        self.session_layout.addWidget(self.session_68_fractionation_plot.web_view)
        self.session_layout.addWidget(self.session_75_fractionation_plot.web_view)
        self.session_layout.addWidget(self.session_76_fractionation_plot.web_view)
        self.session_layout.addWidget(self.session_82_fractionation_plot.web_view)

        self.DH_layout.addWidget(self.DH_68_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_75_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_76_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_82_fractionation_plot.web_view)

        self.all_plots_layout.addLayout(self.DH_layout)
        self.all_plots_layout.addLayout(self.session_layout)
        

        self.manual_calibration_layout.addWidget(self.apply_button)
        self.manual_calibration_layout.addWidget(self.export_button)

        # self.button_layout.addWidget(self.ok_button)
        # self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.manual_calibration_layout)
        self.layout.addLayout(self.all_plots_layout)
        
        # self.layout.addLayout(self.session_layout)
        # self.layout.addLayout(self.DH_layout)
        # self.layout.addLayout(self.button_layout)
    

        # plot the 6/8 session wide fractionation, only plot the selected plateau regions
        # everything from now on will be done on the baseline corrected data
        primary_UPb_standard_total_data = self.baseline_corrected_data[self.baseline_corrected_data['spot_name'].str.contains(self.primary_UPb_standard)]
        primary_UPb_standard_plateau_data = pd.DataFrame()

        # generate a color pd series for the primary standard spots
        self.primary_UPb_standard_spot_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(primary_UPb_standard_total_data['spot_name'].unique()))], index=primary_UPb_standard_total_data['spot_name'].unique())

        # get the primary standard radiogenic isotope ratios (this is standard for zircons as we assume all Pb from zircons are radiogenically produced)
        primary_standard_UPb_data = self.UPb_RM[self.UPb_RM['sample'] == self.primary_UPb_standard]
        # perhaps should put this in a try except block
        try:
            primary_rad_68 = primary_standard_UPb_data['206Pb/238U*'].values[0]
            primary_rad_75 = primary_standard_UPb_data['207Pb/235U*'].values[0]
            primary_rad_76 = primary_standard_UPb_data['207Pb/206Pb*'].values[0]
            primary_rad_82 = primary_standard_UPb_data['208Pb/232Th*'].values[0]
        except:
            print(f'Primary standard {self.primary_UPb_standard} radiogenic isotope ratios not found in reference materials dataframe')
            return
        
    
        # lists to store the downhole ratios of the primary standards
        self.PR_68_DH_times = pd.Series(dtype='float64')
        self.PR_75_DH_times = pd.Series(dtype='float64')
        self.PR_76_DH_times = pd.Series(dtype='float64')
        self.PR_82_DH_times = pd.Series(dtype='float64')
        self.PR_68_DH_ratios = []
        self.PR_75_DH_ratios = []
        self.PR_76_DH_ratios = []
        self.PR_82_DH_ratios = []

        # lists to store the mean and standard deviation of the session wide fractionation
        self.session_standard_68_fractionation_mean = []
        self.session_standard_68_fractionation_std = []
        self.session_standard_75_fractionation_mean = []
        self.session_standard_75_fractionation_std = []
        self.session_standard_76_fractionation_mean = []
        self.session_standard_76_fractionation_std = []
        self.session_standard_82_fractionation_mean = []
        self.session_standard_82_fractionation_std = []

        self.session_standard_time_mean = []

        max_moving_average_window = 1

        # filter for the selected plateau regions
        for spot_name in primary_UPb_standard_total_data['spot_name'].unique():
            spot_data = primary_UPb_standard_total_data[primary_UPb_standard_total_data['spot_name'] == spot_name]
            
            plateau_start_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
            plateau_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
            baseline_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]

            # get data between end of baseline indices and end of plateau indices for DH fractionation calibration
            DH_calibration_data = spot_data[(spot_data['Time'] >= plateau_start_time) & (spot_data['Time'] < plateau_end_time)]
            # get the time in seconds relative to the assumed start time point for the spot
            this_PR_68_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_75_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_76_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_82_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()

            # get the downhole ratios
            this_PR_68_DH_ratios = DH_calibration_data['206Pb']/DH_calibration_data['238U']
            this_PR_75_DH_ratios = DH_calibration_data['207Pb']/DH_calibration_data['235U']
            this_PR_76_DH_ratios = DH_calibration_data['207Pb']/DH_calibration_data['206Pb']
            this_PR_82_DH_ratios = DH_calibration_data['208Pb']/DH_calibration_data['232Th']

            self.PR_68_DH_times = pd.concat([self.PR_68_DH_times, this_PR_68_DH_times])
            self.PR_75_DH_times = pd.concat([self.PR_75_DH_times, this_PR_75_DH_times])
            self.PR_76_DH_times = pd.concat([self.PR_76_DH_times, this_PR_76_DH_times])
            self.PR_82_DH_times = pd.concat([self.PR_82_DH_times, this_PR_82_DH_times])

            self.PR_68_DH_ratios.extend(this_PR_68_DH_ratios)
            self.PR_75_DH_ratios.extend(this_PR_75_DH_ratios)
            self.PR_76_DH_ratios.extend(this_PR_76_DH_ratios)
            self.PR_82_DH_ratios.extend(this_PR_82_DH_ratios)

            # add downhole 68 ratios
            self.DH_68_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times, 
                                                    y=this_PR_68_DH_ratios, 
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name], width=1), 
                                                    mode='lines',
                                                    opacity=0.5,
                                                    name=spot_name))
            # add downhole 75 ratios
            self.DH_75_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_75_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name], width=1), 
                                                    mode='lines',
                                                    opacity=0.5,
                                                    name=spot_name))
            # add downhole 76 ratios
            self.DH_76_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_76_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name], width=1),
                                                    mode='lines',
                                                    opacity=0.5,
                                                    name=spot_name))
            # add downhole 82 ratios
            self.DH_82_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_82_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name], width=1),
                                                    mode='lines',
                                                    opacity=0.5,
                                                    name=spot_name))

        # plot the default exponential fits of the downhole fractionation plots
        self.update_fit_curve('downhole 206Pb/238U')
        self.update_fit_curve('downhole 207Pb/235U')
        self.update_fit_curve('downhole 207Pb/206Pb')
        self.update_fit_curve('downhole 208Pb/232Th')
            

        # print(self.calibration_df)

        # then we need to use the downhole calibration functions to correct for the primary standard, before calculating the session wide fractionation
        for spot_name in primary_UPb_standard_total_data['spot_name'].unique():
            spot_data = primary_UPb_standard_total_data[primary_UPb_standard_total_data['spot_name'] == spot_name]
            
            plateau_start_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
            plateau_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
            baseline_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]

            # more strictly, get selected plateau data for session wide fractionation calibration
            selected_plateau_data = spot_data[(spot_data['Time'] >= plateau_start_time) & (spot_data['Time'] < plateau_end_time)]
            selected_plateau_data_total_seconds = (selected_plateau_data['Time']-selected_plateau_data['Time'].min()).dt.total_seconds()
            # apply downhole calibration by inverting the fitted downhole ratio functions
            # we need to do this for each ratio
            this_DH_corrected_68_ratio = DH_ratio_correction(selected_plateau_data_total_seconds, 
                                                        selected_plateau_data['206Pb']/selected_plateau_data['238U'], 
                                                        self.calibration_df.loc['downhole 206Pb/238U', 'Fit type'],
                                                        self.calibration_df.loc['downhole 206Pb/238U', 'Fit parameters'])
            this_DH_corrected_75_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['207Pb']/selected_plateau_data['235U'],
                                                        self.calibration_df.loc['downhole 207Pb/235U', 'Fit type'],
                                                        self.calibration_df.loc['downhole 207Pb/235U', 'Fit parameters'])
            this_DH_corrected_76_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['207Pb']/selected_plateau_data['206Pb'],
                                                        self.calibration_df.loc['downhole 207Pb/206Pb', 'Fit type'],
                                                        self.calibration_df.loc['downhole 207Pb/206Pb', 'Fit parameters'])
            this_DH_corrected_82_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['208Pb']/selected_plateau_data['232Th'],
                                                        self.calibration_df.loc['downhole 208Pb/232Th', 'Fit type'],
                                                        self.calibration_df.loc['downhole 208Pb/232Th', 'Fit parameters'])
            
            this_DH_corrected_68_ratio_mean = this_DH_corrected_68_ratio.mean()
            this_DH_corrected_75_ratio_mean = this_DH_corrected_75_ratio.mean()
            this_DH_corrected_76_ratio_mean = this_DH_corrected_76_ratio.mean()
            this_DH_corrected_82_ratio_mean = this_DH_corrected_82_ratio.mean()
            this_DH_corrected_68_ratio_std = this_DH_corrected_68_ratio.std()
            this_DH_corrected_75_ratio_std = this_DH_corrected_75_ratio.std()
            this_DH_corrected_76_ratio_std = this_DH_corrected_76_ratio.std()
            this_DH_corrected_82_ratio_std = this_DH_corrected_82_ratio.std()

            self.session_standard_time_mean.append(selected_plateau_data['Time'].mean())

            self.session_standard_68_fractionation_mean.append(this_DH_corrected_68_ratio_mean/primary_rad_68)
            self.session_standard_68_fractionation_std.append(this_DH_corrected_68_ratio_std/primary_rad_68)
            self.session_standard_75_fractionation_mean.append(this_DH_corrected_75_ratio_mean/primary_rad_75)
            self.session_standard_75_fractionation_std.append(this_DH_corrected_75_ratio_std/primary_rad_75)
            self.session_standard_76_fractionation_mean.append(this_DH_corrected_76_ratio_mean/primary_rad_76)
            self.session_standard_76_fractionation_std.append(this_DH_corrected_76_ratio_std/primary_rad_76)
            self.session_standard_82_fractionation_mean.append(this_DH_corrected_82_ratio_mean/primary_rad_82)
            self.session_standard_82_fractionation_std.append(this_DH_corrected_82_ratio_std/primary_rad_82)

            
            # primary_UPb_standard_plateau_data = pd.concat([primary_UPb_standard_plateau_data, selected_plateau_data], axis=0)
        
            # plot session wide 68 fractionation 
            self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=selected_plateau_data['Time'], 
                                                        y=this_DH_corrected_68_ratio/primary_rad_68, 
                                                        line=dict(color='grey'), mode='markers', 
                                                        opacity=0.6, showlegend=False))
            # plot session wide 75 fractionation
            self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=selected_plateau_data['Time'],
                                                        y=this_DH_corrected_75_ratio/primary_rad_75,
                                                        line=dict(color='grey'), mode='markers', 
                                                        opacity=0.6, showlegend=False))
            # plot session wide 76 fractionation
            self.session_76_fractionation_plot.fig.add_trace(go.Scatter(x=selected_plateau_data['Time'],
                                                        y=this_DH_corrected_76_ratio/primary_rad_76,
                                                        line=dict(color='grey'), mode='markers',
                                                        opacity=0.6, showlegend=False))
            # plot session wide 82 fractionation
            self.session_82_fractionation_plot.fig.add_trace(go.Scatter(x=selected_plateau_data['Time'],
                                                        y=this_DH_corrected_82_ratio/primary_rad_82,
                                                        line=dict(color='grey'), mode='markers',
                                                        opacity=0.6, showlegend=False))
            

        # plot the mean and standard deviation of the primary standard's 68 fractionation
        self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=self.session_standard_time_mean,
                                                    y=self.session_standard_68_fractionation_mean,
                                                    error_y=dict(type='data', array=self.session_standard_68_fractionation_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 75 fractionation
        self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=self.session_standard_time_mean,
                                                    y=self.session_standard_75_fractionation_mean,
                                                    error_y=dict(type='data', array=self.session_standard_75_fractionation_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 76 fractionation
        self.session_76_fractionation_plot.fig.add_trace(go.Scatter(x=self.session_standard_time_mean,
                                                    y=self.session_standard_76_fractionation_mean,
                                                    error_y=dict(type='data', array=self.session_standard_76_fractionation_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 82 fractionation
        self.session_82_fractionation_plot.fig.add_trace(go.Scatter(x=self.session_standard_time_mean,
                                                    y=self.session_standard_82_fractionation_mean,
                                                    error_y=dict(type='data', array=self.session_standard_82_fractionation_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        
        # plot the default polynomial degree 3 fits of the session wide fractionation plots
        self.update_fit_curve('session 206Pb/238U')
        self.update_fit_curve('session 207Pb/235U')
        self.update_fit_curve('session 207Pb/206Pb')
        self.update_fit_curve('session 208Pb/232Th')
        


        self.moving_average_window_setting.setMaximum(max_moving_average_window)

        self.DH_68_fractionation_plot.display_plot()
        self.DH_75_fractionation_plot.display_plot()
        self.DH_76_fractionation_plot.display_plot()
        self.DH_82_fractionation_plot.display_plot()

        self.session_68_fractionation_plot.display_plot()
        self.session_75_fractionation_plot.display_plot()
        self.session_76_fractionation_plot.display_plot()
        self.session_82_fractionation_plot.display_plot()

    def update_fit_equation(self):
        fit_type = self.fit_type_dropdown.currentText()
        if fit_type == 'Linear':
            self.fit_equation_text.setText('y = a + bx')
        elif fit_type == 'Exponential':
            self.fit_equation_text.setText('y = a + b*exp(-c*x)')
        elif fit_type == 'Polynomial':
            # dynamically adjust text depending on polynomial degree
            degree = self.poly_degree_setting.value()
            equation_text = 'y = '
            for i in range(degree, 0, -1):
                equation_text += f'a_{i}*x^{i} + '
            equation_text += f'a_0'
            self.fit_equation_text.setText(equation_text)
        elif fit_type == 'Moving Average':
            window_size = self.moving_average_window_setting.value()
            self.fit_equation_text.setText(f'Moving average with window size {window_size} seconds')



    def update_fit_curve(self, this_fractionation):
        # let's assign the x and y values based on the selected fractionation
        if this_fractionation == 'session 206Pb/238U':
            x = self.session_standard_time_mean
            y = self.session_standard_68_fractionation_mean
        elif this_fractionation == 'session 207Pb/235U':
            x = self.session_standard_time_mean
            y = self.session_standard_75_fractionation_mean
        elif this_fractionation == 'session 207Pb/206Pb':
            x = self.session_standard_time_mean
            y = self.session_standard_76_fractionation_mean
        elif this_fractionation == 'session 208Pb/232Th':
            x = self.session_standard_time_mean
            y = self.session_standard_82_fractionation_mean
        elif this_fractionation == 'downhole 206Pb/238U':
            x = self.PR_68_DH_times
            y = self.PR_68_DH_ratios
        elif this_fractionation == 'downhole 207Pb/235U':
            x = self.PR_75_DH_times
            y = self.PR_75_DH_ratios
        elif this_fractionation == 'downhole 207Pb/206Pb':
            x = self.PR_76_DH_times
            y = self.PR_76_DH_ratios
        elif this_fractionation == 'downhole 208Pb/232Th':
            x = self.PR_82_DH_times
            y = self.PR_82_DH_ratios

        reference_time = self.baseline_corrected_data['Time'].min()
        # first clear out previous fit curves and legends
        this_fig = self.calibration_df.loc[this_fractionation, 'Plot'].fig
        this_fig.data = [line for line in this_fig.data if line.name not in ['Linear fit', 'Exponential fit', 'Polynomial fit', 'Moving average fit']]
        this_fit_type = self.calibration_df.loc[this_fractionation, 'Fit type'] 

        if this_fractionation.startswith('session'):
            reference_total_seconds = np.array([(t - reference_time).total_seconds() for t in x])
        else:
            # else this will be the downhole fractionation plot
            # first sort the x and y values based on x
            x, y = zip(*sorted(zip(x.tolist(), y)))
            # do a running mean
            window_size = 0.5
            x, y = moving_average(x, y, window_size)
            this_fig.add_trace(go.Scatter(x=x, y=y, line=dict(color='black', width=2.5), mode='lines', opacity=0.8, name='running mean'))
            reference_total_seconds = np.array(x)

        # print(reference_total_seconds, y)
        if this_fit_type == 'Linear':
            popt, pcov = linear_fit(reference_total_seconds, y)
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'y = {popt[0]:.4f} + {popt[1]:.4f}*x'
            self.calibration_df.at[this_fractionation, 'Fit parameters'] = popt
            self.fit_equation_text.setText(self.calibration_df.loc[this_fractionation, 'Fit equation'])
            self.calibration_df.loc[this_fractionation, 'Quality'] = 1 - np.sum((y - linear(reference_total_seconds, *popt))**2) / np.sum((y - np.mean(y))**2)
            self.quality_label_text.setText(f'R squared value of fit: {self.calibration_df.loc[this_fractionation, "Quality"]:.3f}')
            
            this_fig.add_trace(go.Scatter(x=np.sort(x), y=linear(np.sort(reference_total_seconds), *popt), 
                                          line=dict(color='red', dash='dash', width=2.5), mode='lines', name='Linear fit'))

        elif this_fit_type == 'Exponential':
            popt, pcov = exponential_fit(reference_total_seconds, y)
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'y = {popt[0]:.4f} + {popt[1]:.4f}*exp({-popt[2]:.4f}*x)'

            self.calibration_df.at[this_fractionation, 'Fit parameters'] = popt

            self.fit_equation_text.setText(self.calibration_df.loc[this_fractionation, 'Fit equation'])
            self.calibration_df.loc[this_fractionation, 'Quality'] = 1 - np.sum((y - exponential(reference_total_seconds, *popt))**2) / np.sum((y - np.mean(y))**2)
            self.quality_label_text.setText(f'R squared value of fit: {self.calibration_df.loc[this_fractionation, "Quality"]:.3f}')
            
            this_fig.add_trace(go.Scatter(x=np.sort(x), y=exponential(np.sort(reference_total_seconds), *popt), 
                                              line=dict(color='red', dash='dash', width=2.5), mode='lines', name='Exponential fit'))

        elif this_fit_type == 'Polynomial':
            degree = self.calibration_df.loc[this_fractionation, 'Polynomial degree']
            popt, pcov = poly_fit(reference_total_seconds, y, degree)
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'y = '
            for i in range(degree-1, 0, -1):
                self.calibration_df.at[this_fractionation, 'Fit equation'] += f'{popt[i]:.4f}*x^{i} + '
            self.calibration_df.at[this_fractionation, 'Fit parameters'] = popt
            self.calibration_df.loc[this_fractionation, 'Fit equation'] += f'{popt[0]:.4f}'
            self.fit_equation_text.setText(self.calibration_df.loc[this_fractionation, 'Fit equation'])
            self.calibration_df.loc[this_fractionation, 'Quality'] = 1 - np.sum((y - np.polyval(popt, reference_total_seconds))**2) / np.sum((y - np.mean(y))**2)
            self.quality_label_text.setText(f'R squared value of fit: {self.calibration_df.loc[this_fractionation, "Quality"]:.3f}')

            this_fig.add_trace(go.Scatter(x=np.sort(x), y=np.polyval(popt, np.sort(reference_total_seconds)), 
                                              line=dict(color='red', dash='dash', width=2.5), mode='lines', name='Polynomial fit'))

            
        elif this_fit_type == 'Moving Average':

            window_size = self.calibration_df.loc[this_fractionation, 'Moving average window']
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'Moving average with window size {window_size} seconds'

            # sort reference total seconds and y together based on reference total seconds
            reference_total_seconds, y = zip(*sorted(zip(reference_total_seconds, y)))

            x_moving_average, y_moving_average = moving_average(reference_total_seconds, y, window_size)
            # now interpolate the moving average to the original x values
            y_moving_average = np.interp(reference_total_seconds, x_moving_average, y_moving_average)
            self.calibration_df.loc[this_fractionation, 'Quality'] = 1 - np.sum((y - y_moving_average)**2) / np.sum((y - np.mean(y))**2)
            self.quality_label_text.setText(f'R squared value of fit: {self.calibration_df.loc[this_fractionation, "Quality"]:.3f}')

            this_fig.add_trace(go.Scatter(x=reference_time+pd.Timedelta(seconds=x_moving_average), y=y_moving_average, 
                                          line=dict(color='red', dash='dash', width=2.5), mode='lines', name='Moving average fit'))

    def on_change_fractionation_dropdown(self):
        this_fractionation = self.fractionation_dropdown.currentText()
        # change the fit type to the one stored in the calibration dataframe
        self.fit_type_dropdown.setCurrentText(self.calibration_df.loc[this_fractionation, 'Fit type'])
        self.fit_equation_text.setText(self.calibration_df.loc[this_fractionation, 'Fit equation'])
        
        if self.calibration_df.loc[this_fractionation, 'Fit type'] == 'Polynomial':
            self.poly_degree_setting.setValue(self.calibration_df.loc[this_fractionation, 'Polynomial degree'])
        elif self.calibration_df.loc[this_fractionation, 'Fit type'] == 'Moving Average':
            self.moving_average_window_setting.setValue(self.calibration_df.loc[this_fractionation, 'Moving average window'])

    def on_click_apply_button(self):
        this_fractionation = self.fractionation_dropdown.currentText()
        self.calibration_df.loc[this_fractionation, 'Fit type'] = self.fit_type_dropdown.currentText()
        if self.fit_type_dropdown.currentText() == 'Polynomial':
            self.calibration_df.loc[this_fractionation, 'Polynomial degree'] = self.poly_degree_setting.value()
        elif self.fit_type_dropdown.currentText() == 'Moving Average':
            self.calibration_df.loc[this_fractionation, 'Moving average window'] = self.moving_average_window_setting.value()
        self.update_fit_curve(this_fractionation, 
                              self.baseline_corrected_data['Time'], 
                              self.baseline_corrected_data[this_fractionation])
        

    def on_click_export_button(self):
        # let's start with exporting the isotope ratios that we care about (those that we plot up downhole and session-wide calibrations)
        # let's start with exporting the spot mean results
        # first make a pop up window to ask for the directory to export to
        save_dialog = QFileDialog()
        save_dialog.setFileMode(QFileDialog.Directory)
        save_dialog.setOption(QFileDialog.ShowDirsOnly)
        save_dialog.exec_()
        save_dir = save_dialog.selectedFiles()[0]

        # make a new folder called UPBR_results
        save_dir = os.path.join(save_dir, 'UPBR_results')
        os.makedirs(save_dir, exist_ok=True)

        # make a Time_series folder to store the time series data for each spot
        time_series_dir = os.path.join(save_dir, 'Time_series')
        os.makedirs(time_series_dir, exist_ok=True)

        # make a UPbR_mean_result pandas dataframe to store the spot mean results
        spot_mean_export_df = pd.DataFrame(columns=['Spot Name', 
                                                    '207Pb/235U mean',
                                                    '207Pb/235U se',
                                                    '206Pb/238U mean', 
                                                    '206Pb/238U se',
                                                    'rho 75-68',
                                                    '207Pb/206Pb mean', 
                                                    '207Pb/206Pb se',
                                                    '208Pb/232Th mean',
                                                    '208Pb/232Th se'])

        reference_time = self.baseline_corrected_data['Time'].min()

        # once the user clicks save in the dialog, we start generating the calibration results
        # loop through the baseline_corrected_data and apply the downhole calibration to each spot and apply the session-wide fractionation correction to each spot
        for spot_name in self.baseline_corrected_data['spot_name'].unique():
            # get the spot data
            this_spot_data = self.baseline_corrected_data[self.baseline_corrected_data['spot_name'] == spot_name]

            # apply downhole calibration to each spot
            plateau_start_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
            plateau_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
            baseline_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]

            # get the total plateau data
            selected_plateau_data = this_spot_data[(this_spot_data['Time'] >= plateau_start_time) & (this_spot_data['Time'] < plateau_end_time)]

            # get the time in seconds relative to the assumed start time point for the spot
            selected_plateau_data_total_seconds = (selected_plateau_data['Time']-selected_plateau_data['Time'].min()).dt.total_seconds()

            # apply downhole calibration by inverting the fitted downhole ratio functions
            # we need to do this for each ratio
            this_DH_corrected_68_ratio = DH_ratio_correction(selected_plateau_data_total_seconds, 
                                                        selected_plateau_data['206Pb']/selected_plateau_data['238U'], 
                                                        self.calibration_df.loc['downhole 206Pb/238U', 'Fit type'],
                                                        self.calibration_df.loc['downhole 206Pb/238U', 'Fit parameters'])
            
            this_DH_corrected_75_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['207Pb']/selected_plateau_data['235U'],
                                                        self.calibration_df.loc['downhole 207Pb/235U', 'Fit type'],
                                                        self.calibration_df.loc['downhole 207Pb/235U', 'Fit parameters'])
            
            this_DH_corrected_76_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['207Pb']/selected_plateau_data['206Pb'],
                                                        self.calibration_df.loc['downhole 207Pb/206Pb', 'Fit type'],
                                                        self.calibration_df.loc['downhole 207Pb/206Pb', 'Fit parameters'])
            
            this_DH_corrected_82_ratio = DH_ratio_correction(selected_plateau_data_total_seconds,
                                                        selected_plateau_data['208Pb']/selected_plateau_data['232Th'],
                                                        self.calibration_df.loc['downhole 208Pb/232Th', 'Fit type'],
                                                        self.calibration_df.loc['downhole 208Pb/232Th', 'Fit parameters'])
            
            # apply session wide fractionation correction to the DH corrected ratios
            this_session_corrected_68_ratio = session_ratio_correction((selected_plateau_data['Time']-reference_time).dt.total_seconds(), 
                                                                      this_DH_corrected_68_ratio,
                                                                      self.calibration_df.loc['session 206Pb/238U', 'Fit type'],
                                                                      self.calibration_df.loc['session 206Pb/238U', 'Fit parameters'])
            
            this_session_corrected_75_ratio = session_ratio_correction((selected_plateau_data['Time']-reference_time).dt.total_seconds(),
                                                                        this_DH_corrected_75_ratio,
                                                                        self.calibration_df.loc['session 207Pb/235U', 'Fit type'],
                                                                        self.calibration_df.loc['session 207Pb/235U', 'Fit parameters'])
            
            this_session_corrected_76_ratio = session_ratio_correction((selected_plateau_data['Time']-reference_time).dt.total_seconds(),
                                                                        this_DH_corrected_76_ratio,
                                                                        self.calibration_df.loc['session 207Pb/206Pb', 'Fit type'],
                                                                        self.calibration_df.loc['session 207Pb/206Pb', 'Fit parameters'])
            
            this_session_corrected_82_ratio = session_ratio_correction((selected_plateau_data['Time']-reference_time).dt.total_seconds(),
                                                                        this_DH_corrected_82_ratio,
                                                                        self.calibration_df.loc['session 208Pb/232Th', 'Fit type'],
                                                                        self.calibration_df.loc['session 208Pb/232Th', 'Fit parameters'])
            
            
            # write the time-series data to a csv file for this spot
            time_series_filename = os.path.join(time_series_dir, f'{spot_name}_time_series.csv')
            this_spot_time_series = pd.DataFrame({'Time': selected_plateau_data_total_seconds,   
                                                    '206Pb/238U': this_session_corrected_68_ratio,
                                                    '207Pb/235U': this_session_corrected_75_ratio,
                                                    '207Pb/206Pb': this_session_corrected_76_ratio,
                                                    '208Pb/232Th': this_session_corrected_82_ratio})
            this_spot_time_series.to_csv(time_series_filename, index=False)

            # calculate the spot mean ratios
            spot_mean_68_ratio = this_session_corrected_68_ratio.mean()
            spot_mean_75_ratio = this_session_corrected_75_ratio.mean()
            spot_mean_76_ratio = this_session_corrected_76_ratio.mean()
            spot_mean_82_ratio = this_session_corrected_82_ratio.mean()

            # now let's calculate the standard errors of the mean
            spot_mean_68_ratio_se = this_session_corrected_68_ratio.std()/np.sqrt(len(this_session_corrected_68_ratio))
            spot_mean_75_ratio_se = this_session_corrected_75_ratio.std()/np.sqrt(len(this_session_corrected_75_ratio))
            spot_mean_76_ratio_se = this_session_corrected_76_ratio.std()/np.sqrt(len(this_session_corrected_76_ratio))
            spot_mean_82_ratio_se = this_session_corrected_82_ratio.std()/np.sqrt(len(this_session_corrected_82_ratio))

            # let's calculate the variance of the mean
            # spot_mean_68_ratio_var = this_session_corrected_68_ratio.var()
            # spot_mean_75_ratio_var = this_session_corrected_75_ratio.var()
            # spot_mean_76_ratio_var = this_session_corrected_76_ratio.var()
            # spot_mean_82_ratio_var = this_session_corrected_82_ratio.var()

            # calculate covariance of 75-68
            spot_mean_75_68_cov = np.cov(this_session_corrected_75_ratio, this_session_corrected_68_ratio)
            print(spot_mean_75_68_cov)

            spot_mean_75_68_rho = spot_mean_75_68_cov[0, 1]/np.sqrt(spot_mean_75_68_cov[0, 0]*spot_mean_75_68_cov[1, 1])

            # concat the spot mean ratios to the spot_mean_export_df
            spot_mean_export_df.loc[len(spot_mean_export_df)] = {'Spot Name': spot_name,
                                                                '207Pb/235U mean': spot_mean_75_ratio,
                                                                '207Pb/235U se': spot_mean_75_ratio_se,
                                                                '206Pb/238U mean': spot_mean_68_ratio,
                                                                '206Pb/238U se': spot_mean_68_ratio_se,
                                                                'rho 75-68': spot_mean_75_68_rho,
                                                                '207Pb/206Pb mean': spot_mean_76_ratio,
                                                                '207Pb/206Pb se': spot_mean_76_ratio_se,
                                                                '208Pb/232Th mean': spot_mean_82_ratio, 
                                                                '208Pb/232Th se': spot_mean_82_ratio_se}
            
        # write the spot mean ratios to a csv file
        spot_mean_export_filename = os.path.join(save_dir, 'spot_mean_ratios.csv')
        # sort the spot mean export dataframe based on spot name
        spot_mean_export_df = spot_mean_export_df.sort_values(by='Spot Name', ignore_index=True)
        spot_mean_export_df.to_csv(spot_mean_export_filename, index=False)
