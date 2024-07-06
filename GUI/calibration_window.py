from PyQt5.QtWidgets import QMainWindow, QWidget, QApplication
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QWidget, QPushButton

import pandas as pd
import numpy as np
import sys
import plotly.graph_objects as go

from custom_widgets import *


class CalibrationWindow(QMainWindow):
    def __init__(self, experiment, total_data, primary_UPb_standard, primary_concentration_standard, reference_materials_df):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Standard Calibration Window")
        self.setGeometry(100, 100, 1200, 800)
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
    
        # first plot shows the session wide primary standard fractionation of 6/8 ratios
        self.session_68_fractionation_plot = PlotCanvas()
        self.session_68_fractionation_plot.fig.update_layout(title={'text':r'$\text{Session Wide}\ ^{206}Pb/^{238}U\ \text{Fractionation}$', 
                                                                    'x': 0.5, 
                                                                    'xanchor': 'center'}, 
                                                            yaxis=dict(title=r'$^{206}Pb/^{238}U$ ratio'), 
                                                            legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))

        # second plot shows the session wide primary standard fractionation of 7/5 ratios
        self.session_75_fractionation_plot = PlotCanvas()
        self.session_75_fractionation_plot.fig.update_layout(title={'text':r'$Session Wide\ ^{207}Pb/^{235}U\ Fractionation$', 
                                                            'x': 0.5, 
                                                            'xanchor': 'center'}, 
                                                    yaxis=dict(title=r'$^{207}Pb/^{235}U$ Ratio'), 
                                                    legend=dict(x=0.5, xanchor='center', y=1.2, yanchor='top'))
        # third plot shows the aligned downhole fractionation of 6/8 ratios
        self.DH_68_fractionation_plot = PlotCanvas()
        self.DH_68_fractionation_plot.fig.update_layout(title={'text':r'$Downhole\ ^{206}Pb/^{238}U\ Fractionation$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{206}Pb/^{238}U\ ratio$'),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'))
        
        # fourth plot shows the aligned downhole fractionation of 7/5 ratios
        self.DH_75_fractionation_plot = PlotCanvas()
        self.DH_75_fractionation_plot.fig.update_layout(title={'text':r'$Downhole\ ^{207}Pb/^{235}U\ Fractionation$', 
                                                               'x': 0.5, 
                                                               'xanchor': 'center'},
                                                        yaxis=dict(title=r'$^{207}Pb/^{235}U\ ratio$'),
                                                        xaxis=dict(title='Seconds from start of analysis', 
                                                                   tickformat='.2f'))

        # add ok and cancel button to save or cancel the calibration
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")


        self.layout = QVBoxLayout()
        self.main_widget.setLayout(self.layout)

        self.session_layout = QHBoxLayout()
        self.DH_layout = QHBoxLayout()
        self.button_layout = QHBoxLayout()

        self.session_layout.addWidget(self.session_68_fractionation_plot.web_view)
        self.session_layout.addWidget(self.session_75_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_68_fractionation_plot.web_view)
        self.DH_layout.addWidget(self.DH_75_fractionation_plot.web_view)
        self.button_layout.addWidget(self.ok_button)
        self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.session_layout)
        self.layout.addLayout(self.DH_layout)
        self.layout.addLayout(self.button_layout)
    

        # plot the 6/8 session wide fractionation, only plot the selected plateau regions
        primary_UPb_standard_total_data = self.total_data[self.total_data['spot_name'].str.contains(self.primary_UPb_standard)]
        primary_UPb_standard_plateau_data = pd.DataFrame()

        # generate a color pd series for the primary standard spots
        self.primary_UPb_standard_spot_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(primary_UPb_standard_total_data['spot_name'].unique()))], index=primary_UPb_standard_total_data['spot_name'].unique())

        session_standard_68_mean = []
        session_standard_68_std = []
        session_standard_75_mean = []
        session_standard_75_std = []

        session_standard_time_mean = []

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
            session_standard_68_mean.append((selected_plateau_data['206Pb']/selected_plateau_data['238U']).mean())
            session_standard_68_std.append((selected_plateau_data['206Pb']/selected_plateau_data['238U']).std())
            session_standard_75_mean.append((selected_plateau_data['207Pb']/selected_plateau_data['235U']).mean())
            session_standard_75_std.append((selected_plateau_data['207Pb']/selected_plateau_data['235U']).std())

            primary_UPb_standard_plateau_data = pd.concat([primary_UPb_standard_plateau_data, selected_plateau_data], axis=0)
                                                                        
            self.DH_68_fractionation_plot.fig.add_trace(go.Scatter(x=(DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds(), 
                                                    y=DH_calibration_data['206Pb']/DH_calibration_data['238U'], 
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]), 
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
            self.DH_75_fractionation_plot.fig.add_trace(go.Scatter(x=(DH_calibration_data['Time']-DH_calibration_data['Time'].min()).dt.total_seconds(),
                                                    y=DH_calibration_data['207Pb']/DH_calibration_data['235U'],
                                                    line=dict(color=self.primary_UPb_standard_spot_colors[spot_name]), 
                                                    mode='lines',
                                                    opacity=0.6,
                                                    name=spot_name))
        print(session_standard_time_mean, session_standard_68_mean, session_standard_68_std, session_standard_75_mean, session_standard_75_std)
        self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'], 
                                                    y=primary_UPb_standard_plateau_data['206Pb']/primary_UPb_standard_plateau_data['238U'], 
                                                    line=dict(color='black'), mode='markers', 
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))

        self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=primary_UPb_standard_plateau_data['Time'],
                                                    y=primary_UPb_standard_plateau_data['207Pb']/primary_UPb_standard_plateau_data['235U'],
                                                    line=dict(color='black'), mode='markers', 
                                                    opacity=0.6,
                                                    name=f'{self.primary_UPb_standard} individual analyses'))
        
        self.session_68_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_68_mean,
                                                    error_y=dict(type='data', array=session_standard_68_std*2, visible=True),
                                                    line=dict(color='red'),
                                                    mode='markers',
                                                    opacity=1))
        self.session_75_fractionation_plot.fig.add_trace(go.Scatter(x=session_standard_time_mean,
                                                    y=session_standard_75_mean,
                                                    error_y=dict(type='data', array=session_standard_75_std*2, visible=True),
                                                    line=dict(color='red'),
                                                    mode='markers',
                                                    opacity=1))
        
        self.session_68_fractionation_plot.display_plot()
        self.session_75_fractionation_plot.display_plot()
        self.DH_68_fractionation_plot.display_plot()
        self.DH_75_fractionation_plot.display_plot()
