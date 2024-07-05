from PyQt5.QtWidgets import QMainWindow, QWidget, QApplication
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QWidget, QPushButton

import pandas as pd
import numpy as np
import sys
from custom_widgets import *


class CalibrationWindow(QMainWindow):
    def __init__(self, experiment, total_data, primary_standard):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Data Visualization")
        self.setGeometry(100, 100, 800, 500)
        self.experiment = experiment
        self.total_data = total_data
        self.primary_standard = primary_standard

        self.initUI()

    def initUI(self):
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)
    
        # first plot shows the session wide primary standard fractionation of 6/8 ratios
        self.session_68_fractionation_plot = PlotCanvas()
        self.session_68_fractionation_plot.fig.update_layout(title={'text':'Session Wide 206Pb/238U Fractionation', 'x': 0.5, 'xanchor': 'center'})
        # second plot shows the session wide primary standard fractionation of 7/5 ratios
        self.session_75_fractionation_plot = PlotCanvas()
        self.session_75_fractionation_plot.fig.update_layout(title={'text':'Session Wide 207Pb/235U Fractionation', 'x': 0.5, 'xanchor': 'center'})
        # third plot shows the aligned downhole fractionation of 6/8 ratios
        self.DH_68_fractionation_plot = PlotCanvas()
        self.DH_68_fractionation_plot.fig.update_layout(title={'text':'Downhole 206Pb/238U Fractionation', 'x': 0.5, 'xanchor': 'center'})
        # fourth plot shows the aligned downhole fractionation of 7/5 ratios
        self.DH_75_fractionation_plot = PlotCanvas()
        self.DH_75_fractionation_plot.fig.update_layout(title={'text':'Downhole 207Pb/235U Fractionation', 'x': 0.5, 'xanchor': 'center'})

        # add ok and cancel button to save or cancel the calibration
        self.ok_button = QPushButton("OK")
        self.cancel_button = QPushButton("Cancel")


        self.layout = QVBoxLayout()
        self.session_layout = QHBoxLayout()
        self.DH_layout = QHBoxLayout()
        self.button_layout = QHBoxLayout()

        self.session_layout.addWidget(self.session_68_fractionation_plot)
        self.session_layout.addWidget(self.session_75_fractionation_plot)
        self.DH_layout.addWidget(self.DH_68_fractionation_plot)
        self.DH_layout.addWidget(self.DH_75_fractionation_plot)
        self.button_layout.addWidget(self.ok_button)
        self.button_layout.addWidget(self.cancel_button)

        self.layout.addLayout(self.session_layout)
        self.layout.addLayout(self.DH_layout)
        self.layout.addLayout(self.button_layout)
    

        # plot the 6/8 session wide fractionation, only plot the selected plateau regions
        primary_standard_total_data = self.total_data[self.total_data['spot_name'].str.contains(self.primary_standard)]
        primary_standard_plateau_data = pd.DataFrame()

        # generate a color pd series for the primary standard spots
        self.primary_standard_spot_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(primary_standard_total_data['spot_name'].unique()))], index=primary_standard_total_data['spot_name'].unique())

        # filter for the selected plateau regions
        for spot_name in primary_standard_total_data['spot_name'].unique():
            spot_data = primary_standard_total_data[primary_standard_total_data['spot_name'] == spot_name]
            
            plateau_indices_start = self.experiment[self.experiment['spot_name'] == spot_name]['plateau_indices_start'].values[0]
            plateau_indices_end = self.experiment[self.experiment['spot_name'] == spot_name]['plateau_indices_end'].values[0]
            baseline_indices_end = self.experiment[self.experiment['spot_name'] == spot_name]['baseline_indices_end'].values[0]

            # get data between end of baseline indices and end of plateau indices for DH fractionation calibration
            DH_calibration_data = spot_data.iloc[baseline_indices_end:plateau_indices_end]
            # more strictly, get selected plateau data for session wide fractionation calibration
            selected_plateau_data = spot_data.iloc[plateau_indices_start:plateau_indices_end]

            primary_standard_plateau_data = primary_standard_plateau_data.append(selected_plateau_data)

            self.DH_68_fractionation_plot.add_line(x=DH_calibration_data['Time']-DH_calibration_data['Time'].min(), 
                                                    y=DH_calibration_data['206Pb']/DH_calibration_data['238U'], 
                                                    color=self.primary_standard_spot_colors[spot_name], mode='lines', 
                                                    name=f'{spot_name}')
            self.DH_75_fractionation_plot.add_line(x=DH_calibration_data['Time']-DH_calibration_data['Time'].min(),
                                                    y=DH_calibration_data['207Pb']/DH_calibration_data['235U'],
                                                    color=self.primary_standard_spot_colors[spot_name], mode='markers',
                                                    name=f'{spot_name}')
            
        self.session_68_fractionation_plot.add_line(x=primary_standard_plateau_data['Time'], 
                                                    y=primary_standard_plateau_data['206Pb']/primary_standard_plateau_data['238U'], 
                                                    color='grey', mode='markers', 
                                                    name=f'{self.primary_standard} 206Pb/238U ratio')
        self.session_75_fractionation_plot.add_line(x=primary_standard_plateau_data['Time'],
                                                    y=primary_standard_plateau_data['207Pb']/primary_standard_plateau_data['235U'],
                                                    color='grey', mode='markers',
                                                    name=f'{self.primary_standard} 207Pb/235U ratio')
        

if __name__ == "__main__":

    app = QApplication(sys.argv)
    window = CalibrationWindow()
    window.show()