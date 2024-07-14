from PyQt5.QtWidgets import QMainWindow, QWidget, QApplication 
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QWidget, QPushButton, QComboBox, QSpinBox, QLabel

import pandas as pd
import numpy as np
import sys
import plotly.graph_objects as go

from custom_widgets import *
from util import *



class CalibrationWindow(QMainWindow):
    def __init__(self, experiment, total_data, primary_UPb_standard, primary_concentration_standard, reference_materials_df):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Standard Calibration Window")
        self.setGeometry(100, 100, 1600, 800)
        self.experiment = experiment
        self.total_data = total_data
        self.primary_UPb_standard = primary_UPb_standard
        self.primary_concentration_standard = primary_concentration_standard

        self.concentration_RM = reference_materials_df[reference_materials_df['type'] == 'glass']
        self.UPb_RM = reference_materials_df[reference_materials_df['type'] != 'glass']

        self.initUI()

    def initUI(self):
        # print(self.experiment)
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)

        # initialize the calibration dataframe to keep track of the calibration results
        self.calibration_df = pd.DataFrame()
        # set default fits to the session wide calibration to polynomial fits with degree 3
        self.calibration_df.index = ['session 206Pb/238U', 'session 207Pb/235U', 'session 206Pb/207Pb', 'session 208Pb/232Th', 
                                     'downhole 206Pb/238U', 'downhole 207Pb/235U', 'downhole 206Pb/207Pb', 'downhole 208Pb/232Th']
        self.calibration_df['Fit type'] = ['Polynomial', 'Polynomial', 'Polynomial', 'Polynomial', 
                                           'Exponential', 'Exponential', 'Exponential', 'Exponential']
        self.calibration_df['Fit equation'] = None
        self.calibration_df['Polynomial degree'] = [3,3,3,3,0,0,0,0]
        self.calibration_df['Moving average window'] = None
        self.calibration_df['Quality'] = None

        self.fractionation_dropdown_label = QLabel('Select a ratio to Calibrate')
        self.fractionation_dropdown = QComboBox()
        self.fractionation_dropdown.addItem('session 206Pb/238U')
        self.fractionation_dropdown.addItem('session 207Pb/235U')
        self.fractionation_dropdown.addItem('session 206Pb/207Pb')
        self.fractionation_dropdown.addItem('session 208Pb/232Th')
        self.fractionation_dropdown.addItem('downhole 206Pb/238U')
        self.fractionation_dropdown.addItem('downhole 207Pb/235U')
        self.fractionation_dropdown.addItem('downhole 206Pb/207Pb')
        self.fractionation_dropdown.addItem('downhole 208Pb/232Th')

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

        self.quality_label = QLabel('Quality of fit')
        
        # first plot on top shows the session wide primary standard fractionation of 6/8 ratios
        self.session_68_fractionation_plot = PlotCanvas()
        self.session_68_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{206}Pb/^{238}U\ \text{Fractionation}$', 
                                                                    'x': 0.5, 
                                                                    'xanchor': 'center'}, 
                                                            yaxis=dict(title=r'$^{206}Pb/^{238}U$ ratio', title_font=dict(size=14)), 
                                                            xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                            legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))

        # second plot on top shows the session wide primary standard fractionation of 7/5 ratios
        self.session_75_fractionation_plot = PlotCanvas()
        self.session_75_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{207}Pb/^{235}U\ \text{Fractionation}$', 
                                                            'x': 0.5, 
                                                            'xanchor': 'center'}, 
                                                    yaxis=dict(title=r'$^{207}Pb/^{235}U$ Ratio', title_font=dict(size=14)), 
                                                    xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                    legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))
        
        # third plot on top shows the session wide primary standard fractionation of 6/8 ratios
        self.session_67_fractionation_plot = PlotCanvas()
        self.session_67_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{206}Pb/^{207}Pb\ \text{Fractionation}$',
                                                                    'x': 0.5,
                                                                    'xanchor': 'center'},
                                                            yaxis=dict(title=r'$^{206}Pb/^{207}Pb$ Ratio', title_font=dict(size=14)),
                                                            xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                            legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))
        
        # fourth pllto on top shows the session wide primary standard fractionation of 208Pb 232Th ratios
        self.session_82_fractionation_plot = PlotCanvas()
        self.session_82_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{208}Pb/^{232}Th\ \text{Fractionation}$',
                                                            'x': 0.5, 
                                                            'xanchor': 'center'}, 
                                                    yaxis=dict(title=r'$^{208}Pb/^{232}Th$ Ratio', title_font=dict(size=14)), 
                                                    xaxis=dict(title='Time', title_font=dict(size=12), tickfont=dict(size=10)),
                                                    legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))
        
        # first plot on bottom plot shows the aligned downhole fractionation of 6/8 ratios
        self.DH_68_fractionation_plot = PlotCanvas()
        self.DH_68_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{206}Pb/^{238}U\ \text{Fractionation}$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{206}Pb/^{238}U\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # second plot shows the aligned downhole fractionation of 7/5 ratios
        self.DH_75_fractionation_plot = PlotCanvas()
        self.DH_75_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{207}Pb/^{235}U\ \text{Fractionation}$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{207}Pb/^{235}U\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # third plot shows the aligned downhole fractionation of 7/6 ratios
        self.DH_67_fractionation_plot = PlotCanvas()
        self.DH_67_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{206}Pb/^{207}Pb\ \text{Fractionation}$',
                                                                'x': 0.5,
                                                                'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{206}Pb/^{207}Pb\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis',
                                                                     tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        # fourth plot shows the aligned downhole fractionation of 208Pb 232Th ratios
        self.DH_82_fractionation_plot = PlotCanvas()
        self.DH_82_fractionation_plot.fig.update_layout(title={'text':r'$\text{Downhole}\ ^{208}Pb/^{232}Th\ \text{Fractionation}$',
                                                                'x': 0.5,
                                                                'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{208}Pb/^{232}Th\ \text{ratio}$', title_font=dict(size=14)),
                                                        xaxis=dict(title='Seconds from start of analysis',
                                                                     tickformat='.2f'), 
                                                        legend=dict(font=dict(size=8)))
        
        self.calibration_df['Plot'] = [self.session_68_fractionation_plot, 
                                       self.session_75_fractionation_plot, 
                                       self.session_67_fractionation_plot, 
                                       self.session_82_fractionation_plot,
                                        self.DH_68_fractionation_plot,
                                        self.DH_75_fractionation_plot,
                                        self.DH_67_fractionation_plot,
                                        self.DH_82_fractionation_plot]
        
        # add ok and cancel button to save or cancel the calibration
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")

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
        self.session_layout.addWidget(self.session_67_fractionation_plot.web_view)
        self.session_layout.addWidget(self.session_82_fractionation_plot.web_view)

        self.DH_layout.addWidget(self.DH_68_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_75_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_67_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_82_fractionation_plot.web_view)

        self.all_plots_layout.addLayout(self.session_layout)
        self.all_plots_layout.addLayout(self.DH_layout)

        self.manual_calibration_layout.addWidget(self.ok_button)
        self.manual_calibration_layout.addWidget(self.cancel_button)

        # self.button_layout.addWidget(self.ok_button)
        # self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.manual_calibration_layout)
        self.layout.addLayout(self.all_plots_layout)
        
        # self.layout.addLayout(self.session_layout)
        # self.layout.addLayout(self.DH_layout)
        # self.layout.addLayout(self.button_layout)
    

        # plot the 6/8 session wide fractionation, only plot the selected plateau regions
        primary_UPb_standard_total_data = self.total_data[self.total_data['spot_name'].str.contains(self.primary_UPb_standard)]
        primary_UPb_standard_plateau_data = pd.DataFrame()

        # generate a color pd series for the primary standard spots
        self.primary_UPb_standard_spot_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(primary_UPb_standard_total_data['spot_name'].unique()))], index=primary_UPb_standard_total_data['spot_name'].unique())

        # get the primary standard radiogenic isotope ratios (this is standard for zircons as we assume all Pb from zircons are radiogenically produced)
        primary_standard_UPb_data = self.UPb_RM[self.UPb_RM['sample'] == self.primary_UPb_standard]
        # perhaps should put this in a try except block
        try:
            primary_rad_68 = primary_standard_UPb_data['206Pb/238U*'].values[0]
            primary_rad_75 = primary_standard_UPb_data['207Pb/235U*'].values[0]
            primary_rad_67 = primary_standard_UPb_data['206Pb/207Pb*'].values[0]
            primary_rad_82 = primary_standard_UPb_data['208Pb/232Th*'].values[0]
        except:
            print(f'Primary standard {self.primary_UPb_standard} radiogenic isotope ratios not found in reference materials dataframe')
            return
        
        # lists to store the mean and standard deviation of the session wide fractionation
        session_standard_68_mean = []
        session_standard_68_std = []
        session_standard_75_mean = []
        session_standard_75_std = []
        session_standard_67_mean = []
        session_standard_67_std = []
        session_standard_82_mean = []
        session_standard_82_std = []

        session_standard_time_mean = []

        # lists to store the downhole ratios of the primary standards
        PR_68_DH_times = pd.Series()
        PR_75_DH_times = pd.Series()
        PR_67_DH_times = pd.Series()
        PR_82_DH_times = pd.Series()
        PR_68_DH_ratios = []
        PR_75_DH_ratios = []
        PR_67_DH_ratios = []
        PR_82_DH_ratios = []


        max_moving_average_window = 1

        # filter for the selected plateau regions
        for spot_name in primary_UPb_standard_total_data['spot_name'].unique():
            spot_data = primary_UPb_standard_total_data[primary_UPb_standard_total_data['spot_name'] == spot_name]
            
            plateau_start_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_start_times'].values[0]
            plateau_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['plateau_end_times'].values[0]
            baseline_end_time = self.experiment[self.experiment['spot_names'] == spot_name]['baseline_end_times'].values[0]

            # get data between end of baseline indices and end of plateau indices for DH fractionation calibration
            DH_calibration_data = spot_data[(spot_data['Time'] > baseline_end_time) & (spot_data['Time'] < plateau_end_time)]
            # more strictly, get selected plateau data for session wide fractionation calibration
            selected_plateau_data = spot_data[(spot_data['Time'] >= plateau_start_time) & (spot_data['Time'] < plateau_end_time)]

            session_standard_time_mean.append(selected_plateau_data['Time'].mean())
            session_standard_68_mean.append((selected_plateau_data['206Pb']/selected_plateau_data['238U']).mean()/primary_rad_68)
            session_standard_68_std.append((selected_plateau_data['206Pb']/selected_plateau_data['238U']).std()/primary_rad_68)
            session_standard_75_mean.append((selected_plateau_data['207Pb']/selected_plateau_data['235U']).mean()/primary_rad_75)
            session_standard_75_std.append((selected_plateau_data['207Pb']/selected_plateau_data['235U']).std()/primary_rad_75)
            session_standard_67_mean.append((selected_plateau_data['206Pb']/selected_plateau_data['207Pb']).mean()/primary_rad_67)
            session_standard_67_std.append((selected_plateau_data['206Pb']/selected_plateau_data['207Pb']).std()/primary_rad_67)
            session_standard_82_mean.append((selected_plateau_data['208Pb']/selected_plateau_data['232Th']).mean()/primary_rad_82)
            session_standard_82_std.append((selected_plateau_data['208Pb']/selected_plateau_data['232Th']).std()/primary_rad_82)
            
            primary_UPb_standard_plateau_data = pd.concat([primary_UPb_standard_plateau_data, selected_plateau_data], axis=0)

            max_moving_average_window = max(max_moving_average_window, (DH_calibration_data['Time'].max()-DH_calibration_data['Time'].min()).total_seconds())

            this_PR_68_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_75_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_67_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_82_DH_times = (DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds()
            this_PR_68_DH_ratios = DH_calibration_data['206Pb']/DH_calibration_data['238U']
            this_PR_75_DH_ratios = DH_calibration_data['207Pb']/DH_calibration_data['235U']
            this_PR_67_DH_ratios = DH_calibration_data['206Pb']/DH_calibration_data['207Pb']
            this_PR_82_DH_ratios = DH_calibration_data['208Pb']/DH_calibration_data['232Th']

            PR_68_DH_times = pd.concat([PR_68_DH_times, this_PR_68_DH_times])
            PR_75_DH_times = pd.concat([PR_75_DH_times, this_PR_75_DH_times])
            PR_67_DH_times = pd.concat([PR_67_DH_times, this_PR_67_DH_times])
            PR_82_DH_times = pd.concat([PR_82_DH_times, this_PR_82_DH_times])

            PR_68_DH_ratios.extend(this_PR_68_DH_ratios)
            PR_75_DH_ratios.extend(this_PR_75_DH_ratios)
            PR_67_DH_ratios.extend(this_PR_67_DH_ratios)
            PR_82_DH_ratios.extend(this_PR_82_DH_ratios)


            # add downhole 68 ratios
            self.DH_68_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times, 
                                                    y=this_PR_68_DH_ratios, 
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]), 
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
            # add downhole 75 ratios
            self.DH_75_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_75_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]), 
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
            # add downhole 67 ratios
            self.DH_67_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_67_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]),
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
            # add downhole 82 ratios
            self.DH_82_fractionation_plot.fig.add_trace(go.Scatter(x=this_PR_68_DH_times,
                                                    y=this_PR_82_DH_ratios,
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]),
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
        
        # plot session wide 68 fractionation 
        self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'], 
                                                    y=primary_UPb_standard_plateau_data['206Pb']/primary_UPb_standard_plateau_data['238U']/primary_rad_68, 
                                                    line=dict(color='grey'), mode='markers', 
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))
        # plot session wide 75 fractionation
        self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'],
                                                    y=primary_UPb_standard_plateau_data['207Pb']/primary_UPb_standard_plateau_data['235U']/primary_rad_75,
                                                    line=dict(color='grey'), mode='markers', 
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))
        # plot session wide 67 fractionation
        self.session_67_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'],
                                                    y=primary_UPb_standard_plateau_data['206Pb']/primary_UPb_standard_plateau_data['207Pb']/primary_rad_67,
                                                    line=dict(color='grey'), mode='markers',
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))
        # plot session wide 82 fractionation
        self.session_82_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'],
                                                    y=primary_UPb_standard_plateau_data['208Pb']/primary_UPb_standard_plateau_data['232Th']/primary_rad_82,
                                                    line=dict(color='grey'), mode='markers',
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))
        

        # plot the mean and standard deviation of the primary standard's 68 fractionation
        self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_68_mean,
                                                    error_y=dict(type='data', array=session_standard_68_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 75 fractionation
        self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_75_mean,
                                                    error_y=dict(type='data', array=session_standard_75_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 67 fractionation
        self.session_67_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_67_mean,
                                                    error_y=dict(type='data', array=session_standard_67_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        # plot the mean and standard deviation of the primary standard's 82 fractionation
        self.session_82_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_82_mean,
                                                    error_y=dict(type='data', array=session_standard_82_std*2, visible=True),
                                                    line=dict(color='black'),
                                                    mode='markers',
                                                    showlegend=False,
                                                    opacity=1))
        
        # plot the default polynomial degree 3 fits of the session wide fractionation plots
        self.update_fit_curve('session 206Pb/238U', 
                              primary_UPb_standard_plateau_data['Time'], 
                              primary_UPb_standard_plateau_data['206Pb']/primary_UPb_standard_plateau_data['238U']/primary_rad_68)
        self.update_fit_curve('session 207Pb/235U',
                                primary_UPb_standard_plateau_data['Time'],
                                primary_UPb_standard_plateau_data['207Pb']/primary_UPb_standard_plateau_data['235U']/primary_rad_75)
        self.update_fit_curve('session 206Pb/207Pb',
                                primary_UPb_standard_plateau_data['Time'],
                                primary_UPb_standard_plateau_data['206Pb']/primary_UPb_standard_plateau_data['207Pb']/primary_rad_67)
        self.update_fit_curve('session 208Pb/232Th',
                                primary_UPb_standard_plateau_data['Time'],
                                primary_UPb_standard_plateau_data['208Pb']/primary_UPb_standard_plateau_data['232Th']/primary_rad_82)
        
        # plot the default exponential fits of the downhole fractionation plots
        self.update_fit_curve('downhole 206Pb/238U', 
                              PR_68_DH_times, 
                              PR_68_DH_ratios)
        self.update_fit_curve('downhole 207Pb/235U',
                                PR_75_DH_times,
                                PR_75_DH_ratios)
        self.update_fit_curve('downhole 206Pb/207Pb',
                                PR_67_DH_times,
                                PR_67_DH_ratios)
        self.update_fit_curve('downhole 208Pb/232Th',
                                PR_82_DH_times,
                                PR_82_DH_ratios)
        

        

        self.moving_average_window_setting.setMaximum(max_moving_average_window)

        self.session_68_fractionation_plot.display_plot()
        self.session_75_fractionation_plot.display_plot()
        self.session_67_fractionation_plot.display_plot()
        self.session_82_fractionation_plot.display_plot()

        self.DH_68_fractionation_plot.display_plot()
        self.DH_75_fractionation_plot.display_plot()
        self.DH_67_fractionation_plot.display_plot()
        self.DH_82_fractionation_plot.display_plot()


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



    def update_fit_curve(self, this_fractionation, x, y):
        reference_time = self.total_data['Time'].min()
        # first clear out previous fit curves and legends
        this_fig = self.calibration_df.loc[this_fractionation, 'Plot'].fig
        this_fig.data = [line for line in this_fig.data if line.name not in ['Linear fit', 'Exponential fit', 'Polynomial fit', 'Moving average fit']]
        this_fit_type = self.calibration_df.loc[this_fractionation, 'Fit type'] 

        if this_fractionation.startswith('session'):
            reference_total_seconds = (x-reference_time).apply(lambda x: x.total_seconds())
        else:
            reference_total_seconds = x.to_numpy()

        if this_fit_type == 'Linear':
            self.calibration_df.loc[this_fractionation, 'Fit type'] = 'Linear'
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = 'y = a + bx'
            self.calibration_df.loc[this_fractionation, 'Polynomial degree'] = None
            self.calibration_df.loc[this_fractionation, 'Moving average window'] = None

            popt, pcov = linear_fit(reference_total_seconds, y)
            self.calibration_df.loc[this_fractionation, 'Quality'] = np.sqrt(np.diag(pcov)).mean()
            
            this_fig.add_trace(go.Scatter(x=x, y=linear(reference_total_seconds, *popt), line=dict(color='red', dash='dash'), mode='lines', name='Linear fit'))
            # add text showing the fit equation
            # this_fig.add_annotation(x=0.5, y=0.9, 
            #                         xref='paper', yref='paper', 
            #                         showarrow=False, 
            #                         text=f'y = {popt[1]:.2f}x + {popt[0]:.2f}', 
            #                         font=dict(size=12))

        elif this_fit_type == 'Exponential':
            self.calibration_df.loc[this_fractionation, 'Fit type'] = 'Exponential'
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = 'y = a + b*exp(-c*x)'
            self.calibration_df.loc[this_fractionation, 'Polynomial degree'] = None
            self.calibration_df.loc[this_fractionation, 'Moving average window'] = None

            popt, pcov = exponential_fit(reference_total_seconds, y)
            print(popt, pcov)
            if popt is None:
                self.calibration_df.loc[this_fractionation, 'Quality'] = None
            else:
                self.calibration_df.loc[this_fractionation, 'Quality'] = np.sqrt(np.diag(pcov)).mean()
                this_fig.add_trace(go.Scatter(x=x, y=exponential(reference_total_seconds, *popt), line=dict(color='red', dash='dash'), mode='lines', name='Exponential fit'))
            # add text showing the fit equation
            # this_fig.add_annotation(x=0.5, y=0.9, 
            #                         xref='paper', yref='paper', 
            #                         showarrow=False, 
            #                         text=f'y = {popt[0]:.2f} + {popt[1]:.2f}*exp(-{popt[2]:.2f}*x)', 
            #                         font=dict(size=12))

        elif this_fit_type == 'Polynomial':
            self.calibration_df.loc[this_fractionation, 'Fit type'] = 'Polynomial'
            degree = self.calibration_df.loc[this_fractionation, 'Polynomial degree']
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'y = a_{degree}*x^{degree} + ... + a_0'
            self.calibration_df.loc[this_fractionation, 'Polynomial degree'] = degree
            self.calibration_df.loc[this_fractionation, 'Moving average window'] = None

            popt, pcov = poly_fit(reference_total_seconds, y, degree)
            if popt is None: # this means the algorithm failed to fit the polynomial
                self.calibration_df.loc[this_fractionation, 'Quality'] = None
            else:
                self.calibration_df.loc[this_fractionation, 'Quality'] = np.sqrt(np.diag(pcov)).mean()
                this_fig.add_trace(go.Scatter(x=x, y=np.polyval(popt, reference_total_seconds), line=dict(color='red', dash='dash'), mode='lines', name='Polynomial fit'))
            # add text showing the fit equation
            # equation_text = 'y = '
            # for i in range(degree, 0, -1):
            #     equation_text += f'{popt[i]:.3f}*x^{i} + '
            # equation_text += f'${popt[0]:.3f}$'
            # this_fig.add_annotation(x=0.5, y=0.9, 
            #                         xref='paper', yref='paper', 
            #                         showarrow=False, 
            #                         text=equation_text, 
            #                         font=dict(size=12))
            
        elif this_fit_type == 'Moving Average':
            self.calibration_df.loc[this_fractionation, 'Fit type'] = 'Moving Average'
            window_size = self.calibration_df.loc[this_fractionation, 'Moving average window']
            self.calibration_df.loc[this_fractionation, 'Fit equation'] = f'Moving average with window size {window_size} seconds'
            self.calibration_df.loc[this_fractionation, 'Polynomial degree'] = None
            self.calibration_df.loc[this_fractionation, 'Moving average window'] = window_size

            x_moving_average, y_moving_average = moving_average(reference_total_seconds, y, window_size)
            this_fig.add_trace(go.Scatter
                            (x=reference_time+pd.Timedelta(seconds=x_moving_average), y=y_moving_average, line=dict(color='red', dash='dash'), mode='lines', name='Moving average fit'))
            # add text showing the fit equation
            # this_fig.add_annotation(x=0.5, y=0.9, 
            #                         xref='paper', yref='paper', 
            #                         showarrow=False, 
            #                         text=f'Moving average with window size {window_size} seconds', 
            #                         font=dict(size=12))
            