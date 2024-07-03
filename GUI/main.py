import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWidgets import QWidget, QPushButton, QLabel, QFileDialog, QScrollArea, QMenu, QListWidget
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QAbstractItemView, QTableView

from PyQt5.QtWebEngineWidgets import QWebEngineView

from PyQt5.QtCore import Qt, QPoint, QAbstractTableModel, QVariant
from PyQt5.QtGui import QFont, QColor

from bokeh.plotting import figure, output_file, save, show
from bokeh.resources import INLINE, CDN
from bokeh.embed import file_html
from bokeh.models import ColumnDataSource, DatetimeTickFormatter
from bokeh.io import push_notebook
from bokeh.palettes import Category20, Category10
from bokeh.models.tools import BoxZoomTool, WheelZoomTool  # Import zoom tools
import bokeh

import pandas as pd

import traceback
import util

# initialize global variables
this_experiment = pd.DataFrame()
channels = []
total_data = pd.DataFrame()
total_data_cds = ColumnDataSource()


from bokeh.models import ColumnDataSource
from bokeh.plotting import figure
from bokeh.embed import file_html
from bokeh.resources import CDN
import numpy as np

class PlotCanvas:
    def __init__(self, parent=None):
        self.fig = figure(title="Time Series data", 
                x_axis_label='Time', 
                y_axis_label='Counts/Intensity', 
                x_axis_type='datetime',
                plot_width = 925, 
                plot_height = 500, 
                toolbar_location='below')
        self.web_view = QWebEngineView(parent)
        self.data_source = ColumnDataSource(data={})
        self.time_series_color_index = -1
        # function for initializing the plot with an empty canvas when first loading the data in the self.web_view
        # center the title and make it big font
        self.fig.title.align = 'center'
        self.fig.title.text_font_size = '20pt'

        # show grids
        self.fig.xgrid.grid_line_color = 'gray'
        self.fig.ygrid.grid_line_color = 'gray'

        # add x and y axis labels
        self.fig.xaxis.axis_label_text_font_size = '12pt'
        self.fig.yaxis.axis_label_text_font_size = '12pt'

              # Set x-axis format
        self.fig.xaxis.formatter = DatetimeTickFormatter(
            microseconds=["%m/%d/%Y\n%I:%M:%S %p"],
            milliseconds=["%m/%d/%Y\n%I:%M:%S %p"],
            seconds=["%m/%d/%Y\n%I:%M:%S %p"],
            minsec=["%m/%d/%Y\n%I:%M:%S %p"],
            minutes=["%m/%d/%Y\n%I:%M:%S %p"],
            hourmin=["%m/%d/%Y\n%I:%M:%S %p"],
            hours=["%m/%d/%Y\n%I:%M:%S %p"],
            days=["%m/%d/%Y\n%I:%M:%S %p"],
            months=["%m/%d/%Y\n%I:%M:%S %p"],
            years=["%m/%d/%Y\n%I:%M:%S %p"]
        )

        self.display_plot()


    def clear_plot(self):
        # make an efficient clear plot function that only clears the data source
        self.fig.renderers = []
        # clear the legend
        self.fig.legend.items = []
        self.display_plot()

    def display_plot(self):
        self.html = file_html(self.fig, CDN, "Time Series data")
        self.web_view.setHtml(self.html)

    def add_line(self, x, y, color, line_id):
        # print(self.data_source.data)
        print(self.data_source.column_names)
        self.fig.line('Time', '29Si', source=self.data_source, color=color, legend_label=line_id, name=line_id)
        self.auto_range()
        self.display_plot()

    def remove_line(self, line_id):
        renderers = self.fig.renderers
        for renderer in renderers:
            # print(renderer.name, line_id)
            if renderer.name == line_id:
                self.fig.renderers.remove(renderer)
                break
            # remove the line from the legend
            self.fig.legend.items = [item for item in self.fig.legend.items if item.label['value'] != line_id]
        self.auto_range()
        self.display_plot()

    def auto_range(self, highlighted_channels=[], selected_spot=None):
        if selected_spot == None:
            print(total_data['Time'].min(), total_data['Time'].max())
            self.fig.x_range.start = total_data['Time'].min()
            self.fig.x_range.end = total_data['Time'].max()

            if len(highlighted_channels) != 0:
                self.fig.y_range.start = total_data[highlighted_channels].min().min()
                self.fig.y_range.end = total_data[highlighted_channels].max().max()*1.1
        else:
            self.fig.x_range.start = total_data[total_data['spot_name'] == selected_spot]['Time'].min()
            self.fig.x_range.end = total_data[total_data['spot_name'] == selected_spot]['Time'].max()
            if len(highlighted_channels) != 0:
                self.fig.y_range.start = total_data[total_data['spot_name'] == selected_spot][highlighted_channels].min().min()
                self.fig.y_range.end = total_data[total_data['spot_name'] == selected_spot][highlighted_channels].max().max()*1.1
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

        # button for saving selections
        self.save_selectio_button = QPushButton("Save Selections")
        self.save_selectio_button.clicked.connect(self.save_selections)

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
        # allow multiple selections
        # self.channel_list.setSelectionMode(QListWidget.MultiSelection)
        # add function to highlight the selected channel upon double clicking
        self.channel_list.itemDoubleClicked.connect(self.highlight_channels)
        # allow right click on multiple selections
        self.channel_list.setContextMenuPolicy(Qt.CustomContextMenu)
        # add function to show the context menu
        self.channel_list.customContextMenuRequested.connect(self.show_context_menu)
        # set fixed size for the channel list
        self.channel_list.setFixedSize(200, 200)

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


        # add a table view for displaying the spot data
        self.table_view = QTableView()
        self.model = PandasTableModel(pd.DataFrame())
        self.table_view.setModel(self.model)
        # set initial table view to be fixed size and have the same width as the canvas
        self.table_view.setFixedSize(1200, 250)


        # put the channel list and spot list in a vertical layout
        self.v_layout = QVBoxLayout()
        self.v_layout.addWidget(self.load_button)
        self.v_layout.addWidget(self.save_selectio_button)
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
 
        self.layout.addWidget(self.table_view)
        self.layout.addWidget(self.scroll_area)



    def update_table(self, data):
        self.model = PandasTableModel(data)
        self.table_view.setModel(self.model)

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
        self.update_table(spot_data)

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
                self.plot_canvas.add_line(x='Time', y=item.text(), color=Category10[10][(self.plot_canvas.time_series_color_index+1) % 10], line_id=item.text())
                # self.add_line(item.text(), 'Time', item.text(), color=Category10[10][(self.time_series_color_index+1) % 10])
                self.plot_canvas.time_series_color_index += 1
            # then deselect the item
            item.setSelected(False)

        self.plot_canvas.auto_range()
        self.plot_canvas.display_plot()


    def load_data(self):
            global this_experiment
            global channels
            global total_data
            # if we are opening a new directory, we need to clear the previous data
            if this_experiment.shape[0] != 0:
                this_experiment = pd.DataFrame()
                channels = []
                self.channel_list.clear()
                self.spot_list.clear()
                self.table_view.setModel(PandasTableModel(pd.DataFrame()))
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
                # update the spot list widget
                self.spot_list.clear()
                self.spot_list.addItems(this_experiment['spot_names'])

                self.plot_canvas.data_source.data = total_data

                self.plot_canvas.auto_range()
                # self.plot_canvas.display_plot()

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
