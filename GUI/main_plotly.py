import sys

from PyQt5.QtWidgets import QApplication, QMainWindow, QSizePolicy
from PyQt5.QtWidgets import QWidget, QPushButton, QLabel, QFileDialog, QScrollArea, QMenu, QListWidget, QComboBox, QMessageBox
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QAbstractItemView, QTableView
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import Qt, QPoint, QAbstractTableModel, QVariant
from PyQt5.QtGui import QFont, QColor


import plotly.offline as po
import plotly.graph_objs as go
import plotly.io as pio
import plotly

import pandas as pd
import numpy as np

import traceback
import util
from custom_widgets import *
from calibration_window import CalibrationWindow

# initialize global variables
this_experiment = pd.DataFrame()
channels = []
total_data = pd.DataFrame()
channel_colors = pd.Series()
reference_materials_file_path = './reference_materials/RM.csv'
reference_materials_df = pd.DataFrame()
concentration_RM = pd.DataFrame()
UPb_RM = pd.DataFrame()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Data Visualization")
        self.setGeometry(100, 100, 800, 500)

        global reference_materials_df
        global concentration_RM
        global UPb_RM

        reference_materials_df = pd.read_csv(reference_materials_file_path)
        concentration_RM = reference_materials_df[reference_materials_df['type'] == 'glass']
        UPb_RM = reference_materials_df[reference_materials_df['type'] != 'glass']

        self.initUI()

    def initUI(self):
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)

        # add a menu bar
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        open_action = file_menu.addAction("Open")
        save_action = file_menu.addAction("Save")
        import_action = file_menu.addAction("Import Data")
        export_action = file_menu.addAction("Export Data")
        save_redo_action = file_menu.addAction("Save selections to a .redo file")
        import_redo_action = file_menu.addAction("Import selections from a .redo file")

        Reference_Material_menu = menu.addMenu("Reference Materials")
        RM_setting_action = Reference_Material_menu.addAction("Reference Material Settings")
        # link open action to opening a project file with specific suffix of .UPbR
        open_action.triggered.connect(self.open_project)

        # link save action to saving a project file with specific suffix of .UPbR
        save_action.triggered.connect(self.save_project)

        # link import data action to loading data from a folder
        import_action.triggered.connect(self.load_data)

        # link export data action to exporting data to a folder
        # export_action.triggered.connect(self.export_data)

        # link save .redo file action to saving selections to a .redo file
        # save_redo_action.triggered.connect(self.save_redo)

        # link import .redo file action to importing selections from a .redo file
        # import_redo_action.triggered.connect(self.import_redo)

        self.layout = QVBoxLayout()
        self.main_widget.setLayout(self.layout)

        # set self.fig 
        self.plot_canvas = PlotCanvas()

        # button for loading experiment
        self.load_button = QPushButton("Load Data")
        self.load_button.clicked.connect(self.load_data)
        self.load_button.setFixedSize(150, 40)

        # button for saving selections
        self.save_selection_button = QPushButton("Save Selections")
        self.save_selection_button.clicked.connect(self.save_selections)
        self.save_selection_button.setFixedSize(150, 40)

        # button for auto select baseline and peaks
        self.auto_selection_button = QPushButton("Auto Detect\nBaseline and Peaks!")
        self.auto_selection_button.clicked.connect(self.auto_selection)
        self.auto_selection_button.setFixedSize(150, 50)

        # button for opening up calibration stats window
        self.calibration_button = QPushButton("Calibrate!")
        self.calibration_button.clicked.connect(self.calibrate)
        self.calibration_button.setFixedSize(150, 40)

        # button for exporting data
        self.export_button = QPushButton("Export Data")
        # self.export_button.clicked.connect(self.export_data)
        self.export_button.setFixedSize(150, 40)

        # add a scrollable text area for displaying the console output
        self.text_area = QLabel()
        self.text_area.setWordWrap(True)
        self.text_area.setAlignment(Qt.AlignTop)
        
        # now make it scrollable
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidget(self.text_area)
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidgetResizable(True)  # Allow the scroll area to resize its contents

        # add a list widget for displaying the channels of the data 
        self.channel_list_label = QLabel("Channels")
        self.channel_list_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        self.channel_list = CustomListWidget()
        # add function to highlight the selected channel upon double clicking
        self.channel_list.itemDoubleClicked.connect(self.highlight_channels)
        # allow right click on multiple selections
        self.channel_list.setContextMenuPolicy(Qt.CustomContextMenu)
        # add function to show the context menu
        self.channel_list.customContextMenuRequested.connect(self.show_context_menu)
        # set fixed size for the channel list
        self.channel_list.setFixedSize(200, 200)

        self.primary_channel_label = QLabel("Primary Channel")
        self.primary_channel_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        # add a drop down menu for selecting the primary channel
        self.primary_channel = QComboBox()
        self.primary_channel.setFixedWidth(150)

        self.primary_UPb_standard_label = QLabel("Primary U-Pb\nStandard")
        self.primary_UPb_standard_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        # add a drop down menu for selecting the primary standard
        self.primary_UPb_standard = QComboBox()
        self.primary_UPb_standard.setFixedWidth(150)

        self.primary_concentration_standard_label = QLabel("Primary Concentration\nStandard")
        self.primary_concentration_standard_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        # add a drop down menu for selecting the primary standard
        self.primary_concentration_standard = QComboBox()
        self.primary_concentration_standard.setFixedWidth(150)

        # now add a scrollable list widget for displaying the spot names
        self.spot_list_label = QLabel("Spots")
        self.spot_list_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        self.spot_list = QListWidget()
        # keep the spot list to be single selection
        self.spot_list.setSelectionMode(QListWidget.SingleSelection)
        # do the same thing if it is keyboard selection
        self.spot_list.itemSelectionChanged.connect(self.spot_selection_change)
        
        # set fixed size for the spot list
        self.spot_list.setFixedSize(200, 200)


        # add a table view for displaying the spot selection data
        self.experiment_table_view = QTableView()
        self.experiment_data_model = PandasTableModel(pd.DataFrame())
        self.experiment_table_view.setModel(self.experiment_data_model)
        self.experiment_table_view.setFixedSize(400, 250)
        # add a table view for displaying the spot raw measurement data
        self.spot_table_view = QTableView()
        self.spot_data_model = PandasTableModel(pd.DataFrame())
        self.spot_table_view.setModel(self.spot_data_model)
        # set initial table view to be fixed size and have the same width as the canvas
        self.spot_table_view.setFixedSize(800, 250)

        # put the buttons in one v layout 
        self.button_v_layout = QVBoxLayout()
        self.button_v_layout.addWidget(self.load_button)
        self.button_v_layout.addWidget(self.auto_selection_button)
        self.button_v_layout.addWidget(self.save_selection_button)
        self.button_v_layout.addWidget(self.calibration_button)
        self.button_v_layout.addWidget(self.export_button)
        self.button_v_layout.addWidget(self.primary_channel_label)
        self.button_v_layout.addWidget(self.primary_channel)
        self.button_v_layout.addWidget(self.primary_UPb_standard_label)
        self.button_v_layout.addWidget(self.primary_UPb_standard)
        self.button_v_layout.addWidget(self.primary_concentration_standard_label)
        self.button_v_layout.addWidget(self.primary_concentration_standard)

        # put the channel list and spot list in a vertical layout
        self.v_layout = QVBoxLayout()
        self.v_layout.addWidget(self.channel_list_label)
        self.v_layout.addWidget(self.channel_list)
        self.v_layout.addWidget(self.spot_list_label)
        self.v_layout.addWidget(self.spot_list)

        # put the vertical layout and the canvas in a horizontal layout
        self.h_layout = QHBoxLayout()
        self.h_layout.addLayout(self.button_v_layout)
        self.h_layout.addLayout(self.v_layout)
        self.h_layout.addWidget(self.plot_canvas.web_view)
        # make sure the web_view is filled by the canvas
        self.h_layout.setStretch(1, 1)

        self.layout.addLayout(self.h_layout)
        self.table_h_layout = QHBoxLayout()
        self.table_h_layout.addWidget(self.experiment_table_view)
        self.table_h_layout.addWidget(self.spot_table_view)
        self.layout.addLayout(self.table_h_layout)

        self.layout.addWidget(self.scroll_area)

    def calibrate(self):
        # function for opening up the calibration window
        global this_experiment
        global total_data
        global reference_materials_df

        self.calibration_window = CalibrationWindow(this_experiment, 
                                                    total_data, 
                                                    self.primary_UPb_standard.currentText(), 
                                                    self.primary_concentration_standard.currentText(), 
                                                    reference_materials_df)
        self.calibration_window.show()
        
    def auto_selection(self):
        global this_experiment

        # if there are already selections in the experiment pop up a window that asks if the user wants to overwrite the selections
        if this_experiment[['baseline_start_times', 'baseline_end_times', 'plateau_start_times', 'plateau_end_times']].notnull().all().all():
            self.update_console('There are already selections in the experiment, do you want to overwrite them?')
            # add a pop up window that asks if the user wants to overwrite the selections
            # if the user clicks yes, then overwrite the selections
            # if the user clicks no, then do nothing
            # if the user clicks cancel, then do nothing

            msg_box = QMessageBox()
            msg_box.setWindowTitle("Auto Detect Baseline and Peaks")
            msg_box.setText("Baseline and peaks have already been selected. Do you want to overwrite them?")
            msg_box.setIcon(QMessageBox.Question)
            msg_box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)

            # Show the message box and capture the response
            response = msg_box.exec_()

            if response == QMessageBox.No:
                # do nothing
                return
            else: 
                self.primary_channel_name = self.primary_channel.currentText()
                self.update_console(f'Auto selecting baseline and peaks based on primary channel: {self.primary_channel_name}')
                # we loop through all the spots and find the baseline and peaks
                this_experiment = util.auto_select_baseline_peak(this_experiment, total_data, self.primary_channel_name)
                self.update_experiment_table(this_experiment)
                self.update_console('Auto selection complete')
                # if there is currently selected channel and spot, plot the selection
                self.update_selection()  
        else:
            self.primary_channel_name = self.primary_channel.currentText()
            self.update_console(f'Auto selecting baseline and peaks based on primary channel: {self.primary_channel_name}')
            # we loop through all the spots and find the baseline and peaks
            this_experiment = util.auto_select_baseline_peak(this_experiment, total_data, self.primary_channel_name)
            self.update_experiment_table(this_experiment)
            self.update_console('Auto selection complete')
            # if there is currently selected channel and spot, plot the selection
            self.update_selection()

    def update_experiment_table(self, data):
        self.experiment_data_model = PandasTableModel(data)
        self.experiment_table_view.setModel(self.experiment_data_model)

    def update_spot_table(self, data):
        self.spot_data_model = PandasTableModel(data)
        self.spot_table_view.setModel(self.spot_data_model)

    def spot_selection_change(self):
        global this_experiment
        global total_data
        # highlighted_channels = self.get_highlighted_channels()
        # deal with the case where we are opening a new directory when there is already data
        # which causes the spot list selection to be empty and the table view to be empty
        # get the spot name from the selected item
        selected_items = self.spot_list.selectedItems()[0]
        spot_name = selected_items.text()
        # get the og spot's data from the experiment
        spot_data = total_data[total_data['spot_name'] == spot_name].reset_index(drop=True)
        # update the table view with the spot's data
        self.update_spot_table(spot_data)

        self.update_console(f' Viewing data of selected spot: {spot_name}')
        # clear the plot
        self.plot_canvas.clear_plot()
        # add the lines for the highlighted channels
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            if item.background().color() == QColor(Qt.yellow):
                self.plot_canvas.add_line(x='Time', y=item.text(), data=spot_data, color=channel_colors.loc[item.text()])
        self.update_selection()
        self.plot_canvas.auto_range(total_data, self.get_highlighted_channels(), spot_name)
        self.plot_canvas.display_plot()
            
    def get_highlighted_channels(self):
        highlighted_channels = []
        for i in range(self.channel_list.count()):
            item = self.channel_list.item(i)
            # if the item background is not default color
            if item.background().color().name() not in ['#000000', '#ffffff']:
                highlighted_channels.append(item.text())
        return highlighted_channels

    def show_context_menu(self, pos: QPoint):
        contextMenu = QMenu(self)

        highlightAction = contextMenu.addAction("Highlight Selected")
        highlightAction.triggered.connect(self.highlight_channels)

        contextMenu.exec_(self.channel_list.mapToGlobal(pos))

    def update_selection(self):
        global this_experiment
        # function for updating the selection boxes on the canvas
        # if there are previously plotted selections, remove them
        self.plot_canvas.remove_selection()
        # if the primary channel is highlighted, plot the selection for the primary channel
        if (self.primary_channel.currentText() in self.get_highlighted_channels()) and (len(self.spot_list.selectedItems()) != 0):
            spot_name = self.spot_list.selectedItems()[0].text()
            spot_data = total_data[total_data['spot_name'] == spot_name].reset_index(drop=True)
            spot_selection = this_experiment[this_experiment['spot_names'] == spot_name]
            # if there is selected baseline and plateau, draw them on the plot
            if spot_selection[['baseline_start_times', 'baseline_end_times', 'plateau_start_times', 'plateau_end_times']].notnull().all().all():
                # use the start and end times to filter for the baseline and plateau data
                baseline_data = spot_data[(spot_data['Time'] >= spot_selection['baseline_start_times'].values[0]) & (spot_data['Time'] < spot_selection['baseline_end_times'].values[0])]
                plateau_data = spot_data[(spot_data['Time'] >= spot_selection['plateau_start_times'].values[0]) & (spot_data['Time'] < spot_selection['plateau_end_times'].values[0])]
                # get the mean and 2 standard deviations of the baseline and plateau
                baseline_mean = baseline_data[self.primary_channel.currentText()].mean()
                baseline_std2 = 2*baseline_data[self.primary_channel.currentText()].std()
                plateau_mean = plateau_data[self.primary_channel.currentText()].mean()
                plateau_std2 = 2*plateau_data[self.primary_channel.currentText()].std()
                # draw the baseline and plateau on the plot
                self.plot_canvas.draw_selection(spot_selection['baseline_start_times'].values[0], spot_selection['baseline_end_times'].values[0], baseline_mean, baseline_std2, 'blue', 'lightblue', selection_type='baseline')
                self.plot_canvas.draw_selection(spot_selection['plateau_start_times'].values[0], spot_selection['plateau_end_times'].values[0], plateau_mean, plateau_std2, 'red', 'pink', selection_type='plateau')
            

    def highlight_channels(self):
        # upon double clicking a channel, highlight the corresponding channel in the project if it was not already highlighted
        # if it was already highlighted, remove the highlight
        global this_experiment
        global total_data
        for item in self.channel_list.selectedItems():
            if item.background().color() == QColor(Qt.yellow):
                item.setBackground(QColor(Qt.white))
                try:
                    self.plot_canvas.remove_line(item.text())
                    # if the de-highlighted channel is the primary channel, remove the selections as well
                    if item.text() == self.primary_channel.currentText():
                        self.plot_canvas.remove_selection()
                    if len(self.spot_list.selectedItems()) != 0:
                        self.plot_canvas.auto_range(total_data, self.get_highlighted_channels(), self.spot_list.selectedItems()[0].text())
                except Exception as e:
                    self.update_console(f'Error removing line: {e}')
            else:
                item.setBackground(QColor(Qt.yellow))
                if len(self.spot_list.selectedItems()) == 0:
                    self.update_console('No spot selected, auto range to the entire data set')
                    self.plot_canvas.auto_range(total_data, self.get_highlighted_channels())
                else:
                    spot_name = self.spot_list.selectedItems()[0].text()
                    spot_data = total_data[total_data['spot_name'] == spot_name].reset_index(drop=True)
                    self.plot_canvas.add_line(x='Time', y=item.text(), data=spot_data, color=channel_colors.loc[item.text()])

                    self.update_selection()
                    self.plot_canvas.auto_range(total_data, self.get_highlighted_channels(), spot_name)
            # then deselect the item
            item.setSelected(False)
        self.plot_canvas.display_plot()


    def load_data(self):
            global this_experiment
            global channels
            global total_data
            global channel_colors
            global reference_materials_df
            global concentration_RM
            global UPb_RM

            # if we are opening a new directory, we need to clear the previous data
            if this_experiment.shape[0] != 0:
                this_experiment = pd.DataFrame()
                channels = []
                self.channel_list.clear()
                self.spot_list.clear()
                self.primary_channel.clear()
                self.primary_UPb_standard.clear()
                self.primary_concentration_standard.clear()
                self.spot_table_view.setModel(PandasTableModel(pd.DataFrame()))
                self.experiment_table_view.setModel(PandasTableModel(pd.DataFrame()))
                self.plot_canvas.clear_plot()
                self.update_console('Cleared previous data')

            # make sure to only select folders
            options = QFileDialog.Options()
            directory = QFileDialog.getExistingDirectory(self, "Select Folder", "", options=options)
            try:
                this_experiment, channels, total_data = util.read_experiment_data(directory)
                self.update_console(f'Loaded data from {this_experiment.shape[0]} spots from {directory}')
                # update the channel list widget
                self.channel_list.clear()
                self.channel_list.addItems(channels)
                self.primary_channel.addItems(channels)
                # add default selection to be the last channel
                self.primary_channel.setCurrentIndex(len(channels)-1)

                self.concentration_standards_used = [i for i in this_experiment['sample'].unique() if i in concentration_RM['sample'].unique()]
                self.UPb_standards_used = [i for i in this_experiment['sample'].unique() if i in UPb_RM['sample'].unique()]
                self.primary_concentration_standard.addItems(self.concentration_standards_used)
                self.primary_UPb_standard.addItems(self.UPb_standards_used)

                # update the spot list widget
                self.spot_list.clear()
                self.spot_list.addItems(this_experiment['spot_names'])

                # make a list of discrete colors for the channels
                channel_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(channels))], index=channels)

                # show the experiment table view
                self.update_experiment_table(this_experiment)

                self.plot_canvas.auto_range(total_data)


            except Exception as e:
                # print out the entire error message in the console
                self.update_console(f'Error loading data from {directory}: \n')
                exception_details = traceback.format_exception(type(e), e, e.__traceback__)
                for detail in exception_details:
                    self.update_console(detail)

    def save_selections(self):
        return 
    
    # here is a place holder for the open project function
    def open_project(self):
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getOpenFileName(self, "Open Project", "", "UPbR Files (*.UPbR)", options=options)
        if file_name:
            print(file_name)

    # here is a place holder for the save project function
    def save_project(self):
        options = QFileDialog.Options()
        file_name, _ = QFileDialog.getSaveFileName(self, "Save Project", "", "UPbR Files (*.UPbR)", options=options)
        self.update_console(self, f'Saved project to: {file_name}')


    # make a function that keeps updating the text area
    def update_console(self, text):
        if self.text_area.text() == '':
            self.text_area.setText(text)
        else:
            self.text_area.setText(self.text_area.text() + '\n' + text)


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
