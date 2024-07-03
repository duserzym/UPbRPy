import sys
import threading

from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWidgets import QWidget, QPushButton, QLabel, QFileDialog, QScrollArea, QMenu, QListWidget, QComboBox
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

import traceback
import util

# initialize global variables
this_experiment = pd.DataFrame()
channels = []
total_data = pd.DataFrame()
channel_colors = pd.Series()


import numpy as np

class PlotCanvas:
    def __init__(self, parent=None):


        self.web_view = QWebEngineView(parent)


        self.layout = go.Layout(xaxis=dict(title='Time'), yaxis=dict(title='Count/Intensity'))
        # set plot title
        self.layout.update(title={'text':'Time Series Data', 'x': 0.5, 'xanchor': 'center'})
        self.fig = go.Figure(data=[], layout=self.layout)
        # Set x-axis format
        self.fig.update_xaxes(tickformat="%m/%d/%Y\n%I:%M:%S %p")
        # add legend
        self.fig.update_layout(showlegend=True)

        self.display_plot()


    def clear_plot(self):
        # clear the plot by removing all the renderers
        self.fig.data = []

        self.display_plot()

    def display_plot(self):
        # Convert Plotly figure to HTML
        self.html = '<html><head><meta charset="utf-8" />'
        self.html += '<script src="https://cdn.plot.ly/plotly-latest.min.js"></script></head>'
        self.html += '<body>'
        self.html += pio.to_html(self.fig, full_html=False, include_plotlyjs=False)
        # self.html += po.plot(self.fig, include_plotlyjs=False, output_type='div')
        self.html += '</body></html>'
        self.web_view.setHtml(self.html)

    def add_line(self, x, y, color):
        self.fig.add_trace(go.Scatter
            (x=total_data[x], y=total_data[y], mode='lines', name=y, line=dict(color=color)))
        
        self.auto_range()
        self.display_plot()

    def remove_line(self, line_id):
        # remove the line with the name of line_id
        self.fig.data = [line for line in self.fig.data if line.name != line_id]

        self.auto_range()
        self.display_plot()

    def auto_range(self, highlighted_channels=[], selected_spot=None):
        if selected_spot == None:
            # set plotly x range to be the min and max of the total data
            self.fig.update_xaxes(range=[total_data['Time'].min(), total_data['Time'].max()])
            # set plotly y range to be the min and max of the total data
            self.fig.update_yaxes(range=[total_data[highlighted_channels].min().min(), total_data[highlighted_channels].max().max()*1.1])

            if len(highlighted_channels) != 0:
                self.fig.update_yaxes(range=[total_data[highlighted_channels].min().min(), total_data[highlighted_channels].max().max()*1.1])
        else:
            self.fig.update_xaxes(range=[total_data[total_data['spot_name'] == selected_spot]['Time'].min(), total_data[total_data['spot_name'] == selected_spot]['Time'].max()])

            if len(highlighted_channels) != 0:
                self.fig.update_yaxes(range=[total_data[total_data['spot_name'] == selected_spot][highlighted_channels].min().min(), total_data[total_data['spot_name'] == selected_spot][highlighted_channels].max().max()*1.1])
        self.display_plot()

class PandasTableModel(QAbstractTableModel):
    def __init__(self, data):
        super().__init__()
        self._data = data

    def data(self, index, role):
        if role == Qt.DisplayRole:
            value = self._data.iloc[index.row(), index.column()]
            return str(value)
        return QVariant()

    def rowCount(self, index):
        return self._data.shape[0]

    def columnCount(self, index):
        return self._data.shape[1]

    def headerData(self, section, orientation, role):
        if role == Qt.DisplayRole:
            if orientation == Qt.Horizontal:
                return str(self._data.columns[section])
            else:
                return str(self._data.index[section])
        return QVariant()
    
class CustomListWidget(QListWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            modifiers = QApplication.keyboardModifiers()
            if not (modifiers & Qt.ControlModifier or modifiers & Qt.ShiftModifier):
                item = self.itemAt(event.pos())
                if item is not None:
                    self.clearSelection()
                    item.setSelected(True)
        super().mousePressEvent(event)

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Data Visualization")
        self.setGeometry(100, 100, 1000, 600)

        self.initUI()

    def initUI(self):
        self.main_widget = QWidget()
        self.setCentralWidget(self.main_widget)

        # add a menu bar
        menu = self.menuBar()
        file_menu = menu.addMenu("File")
        # add open project action
        open_action = file_menu.addAction("Open")
        # add save project action
        save_action = file_menu.addAction("Save")
        # add import data action
        import_action = file_menu.addAction("Import Data")
        # add export data action
        export_action = file_menu.addAction("Export Data")
        # add save .redo file action
        save_redo_action = file_menu.addAction("Save selections to a .redo file")
        # add import .redo file action
        import_redo_action = file_menu.addAction("Import selections from a .redo file")

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

        # button for auto select baseline and peaks
        self.auto_selection_button = QPushButton("Auto Detect\nBaseline and Peaks!")
        self.auto_selection_button.clicked.connect(self.auto_selection)

        # button for saving selections
        self.save_selection_button = QPushButton("Save Selections")
        self.save_selection_button.clicked.connect(self.save_selections)

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
        # add a drop down menu for selecting the primary channel
        self.primary_channel = QComboBox()

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


        # put the channel list and spot list in a vertical layout
        self.v_layout = QVBoxLayout()
        self.v_layout.addWidget(self.load_button)
        self.v_layout.addWidget(self.save_selection_button)
        self.v_layout.addWidget(self.auto_selection_button)
        self.v_layout.addWidget(self.channel_list_label)
        self.v_layout.addWidget(self.channel_list)
        self.v_layout.addWidget(self.spot_list_label)
        self.v_layout.addWidget(self.spot_list)

        # put the vertical layout and the canvas in a horizontal layout
        self.h_layout = QHBoxLayout()
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

    def auto_selection(self):
        self.primary_channel_name = self.primary_channel.currentText()
        self.update_console(f'Auto selecting baseline and peaks based on primary channel: {self.primary_channel_name}')

    def update_experiment_table(self, data):
        self.experiment_data_model = PandasTableModel(data)
        self.experiment_table_view.setModel(self.experiment_data_model)

    def update_spot_table(self, data):
        self.spot_data_model = PandasTableModel(data)
        self.spot_table_view.setModel(self.spot_data_model)

    def spot_selection_change(self):
        global this_experiment
        # highlighted_channels = self.get_highlighted_channels()
        # deal with the case where we are opening a new directory when there is already data
        # which causes the spot list selection to be empty and the table view to be empty
        # get the spot name from the selected item
        selected_items = self.spot_list.selectedItems()[0]
        spot_name = selected_items.text()
        # get the spot's path from the experiment dataframe
        spot_path = this_experiment[this_experiment['spot_names'] == spot_name]['spot_paths'].values[0]
        # get the og spot's data from the experiment
        spot_data = util.read_spot_data(spot_path)
        # update the table view with the spot's data
        self.update_spot_table(spot_data)

        self.update_console(f' Viewing data of selected spot: {spot_name}')

        self.plot_canvas.auto_range(self.get_highlighted_channels(), spot_name)
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

    def highlight_channels(self):
        # upon double clicking a channel, highlight the corresponding channel in the project if it was not already highlighted
        # if it was already highlighted, remove the highlight
        for item in self.channel_list.selectedItems():
            if item.background().color() == QColor(Qt.yellow):
                item.setBackground(QColor(Qt.white))
                try:
                    self.plot_canvas.remove_line(item.text())
                except Exception as e:
                    self.update_console(f'Error removing line: {e}')
            else:
                item.setBackground(QColor(Qt.yellow))
                self.plot_canvas.add_line(x='Time', y=item.text(), color=channel_colors.loc[item.text()])
            # then deselect the item
            item.setSelected(False)

        self.plot_canvas.auto_range(self.get_highlighted_channels(), self.spot_list.selectedItems()[0])
        self.plot_canvas.display_plot()


    def load_data(self):
            global this_experiment
            global channels
            global total_data
            global channel_colors
            # if we are opening a new directory, we need to clear the previous data
            if this_experiment.shape[0] != 0:
                this_experiment = pd.DataFrame()
                channels = []
                self.channel_list.clear()
                self.spot_list.clear()
                self.spot_table_view.setModel(PandasTableModel(pd.DataFrame()))
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
                # update the spot list widget
                self.spot_list.clear()
                self.spot_list.addItems(this_experiment['spot_names'])

                # make a list of discrete colors for the channels
                channel_colors = pd.Series([f'#{i:06x}' for i in np.random.randint(0, 0xFFFFFF, len(channels))], index=channels)

                # show the experiment table view
                self.update_experiment_table(this_experiment)

                self.plot_canvas.auto_range()


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
