import sys
from PyQt5.QtWidgets import QApplication, QMainWindow
from PyQt5.QtWidgets import QWidget, QPushButton, QLabel, QFileDialog, QScrollArea, QMenu, QListWidget
from PyQt5.QtWidgets import QVBoxLayout, QHBoxLayout
from PyQt5.QtWidgets import QAbstractItemView, QTableView

from PyQt5.QtWebEngineWidgets import QWebEngineView

from PyQt5.QtCore import Qt, QPoint, QAbstractTableModel, QVariant
from PyQt5.QtGui import QFont, QColor

from bokeh.plotting import figure, output_file, save, show
from bokeh.resources import INLINE
from bokeh.embed import file_html
from bokeh.models import ColumnDataSource
from bokeh.io import push_notebook

# from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
# from matplotlib.figure import Figure
# from matplotlib.widgets import RectangleSelector

import pandas as pd

import traceback
import util

this_experiment = pd.DataFrame()
channels = []

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

# class PlotCanvas(FigureCanvas):
#     def __init__(self, parent=None, width=8, height=6, dpi=150):
#         fig = Figure(figsize=(width, height), dpi=dpi)
#         self.axes = fig.add_subplot(111)
#         super().__init__(fig)
#         self.setParent(parent)
#         self.axes.set_xlabel('Time')
#         self.axes.set_ylabel('Counts/Intensity')

#     def scatter(self, x, y, **kwargs):
#         self.axes.scatter(x, y, **kwargs)
#         self.draw()

#     def plot(self, x, y, **kwargs):
#         self.axes.plot(x, y, **kwargs)
#         self.draw()

#     # function to clear the canvas
#     def clear(self):
#         self.axes.clear()
#         self.draw()

#     # function to show the legend
#     def show_legend(self):
#         self.axes.legend()
#         self.draw()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Data Visualization")
        self.setGeometry(100, 100, 1000, 800)

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

        # self.plot_canvas = PlotCanvas(self, width=8, height=6)
        self.web_view = QWebEngineView()

        self.load_button = QPushButton("Load Data")
        self.load_button.clicked.connect(self.load_data)

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
        self.channel_list.itemDoubleClicked.connect(self.highlight_channel)
        # allow right click on multiple selections
        self.channel_list.setContextMenuPolicy(Qt.CustomContextMenu)
        # add function to show the context menu
        self.channel_list.customContextMenuRequested.connect(self.show_context_menu)
        # set fixed size for the channel list
        self.channel_list.setFixedSize(200, 250)

        # now add a scrollable list widget for displaying the spot names
        self.spot_list_label = QLabel("Spots")
        self.spot_list_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        self.spot_list = QListWidget()
        # keep the spot list to be single selection
        self.spot_list.setSelectionMode(QListWidget.SingleSelection)
        # do the same thing if it is keyboard selection
        self.spot_list.itemSelectionChanged.connect(self.display_spot_data)
        
        # set fixed size for the spot list
        self.spot_list.setFixedSize(200, 250)


        # add a table view for displaying the spot data
        self.table_view = QTableView()
        self.model = PandasTableModel(pd.DataFrame())
        self.table_view.setModel(self.model)
        # set initial table view to be fixed size and have the same width as the canvas
        self.table_view.setFixedSize(1200, 250)


        # put the channel list and spot list in a vertical layout
        self.v_layout = QVBoxLayout()
        self.v_layout.addWidget(self.load_button)
        self.v_layout.addWidget(self.channel_list_label)
        self.v_layout.addWidget(self.channel_list)
        self.v_layout.addWidget(self.spot_list_label)
        self.v_layout.addWidget(self.spot_list)

        # put the vertical layout and the canvas in a horizontal layout
        self.h_layout = QHBoxLayout()
        self.h_layout.addLayout(self.v_layout)
        self.h_layout.addWidget(self.web_view)
        # make sure the web_view is filled by the canvas
        self.h_layout.setStretch(1, 1)


        self.layout.addLayout(self.h_layout)
 
        self.layout.addWidget(self.table_view)
        self.layout.addWidget(self.scroll_area)


        self.initialize_plot()

    def initialize_plot(self):
        # function for initializing the plot with an empty canvas when first loading the data in the self.web_view
        
        plot = figure(title="Time Series data", 
                      x_axis_label='Time', 
                      y_axis_label='Counts/Intensity', 
                      plot_width = 925, 
                      plot_height = 550)
        # center the title and make it big font
        plot.title.align = 'center'
        plot.title.text_font_size = '20pt'

        plot.scatter([0,0], [0,0], color=None)
        # show grids
        plot.xgrid.grid_line_color = 'gray'
        plot.ygrid.grid_line_color = 'gray'

        # add x and y axis labels
        plot.xaxis.axis_label_text_font_size = '12pt'
        plot.yaxis.axis_label_text_font_size = '12pt'

        html = file_html(plot, INLINE, "Time Series data")
        self.web_view.setHtml(html)


    # def create_plot(self, experiment):
    #     global time_series_data_source
    #     global time_series_data_handle
    #     time_series_data_source = ColumnDataSource(data=dict(x=[], y=[]))
    #     plot = figure(title="Time Series", x_axis_label='Time', y_axis_label='Counts/Intensity')
    #     plot.scatter('x', 'y', source=time_series_data_source)
    #     time_series_data_handle = show(plot, notebook_handle=True)
    #     return plot

    # # Update plot
    # def update_plot(x, y):
    #     global time_series_data_source
    #     global time_series_data_handle
    #     time_series_data_source.data = dict(x=x, y=y)
    #     push_notebook(handle=time_series_data_handle)


    def display_spot_data(self):
        global this_experiment
        # deal with the case where we are opening a new directory when there is already data
        # which causes the spot list selection to be empty and the table view to be empty
        if len(self.spot_list.selectedItems()) != 0:
            # get the spot name from the selected item
            selected_items = self.spot_list.selectedItems()[0]
            spot_name = selected_items.text()
            # get the spot's path from the experiment dataframe
            spot_path = this_experiment[this_experiment['spot_names'] == spot_name]['spot_paths'].values[0]
            # get the spot's data from the experiment
            spot_data = util.read_spot_data(spot_path)
            # set the model of the table view to the spot's data
            self.model = PandasTableModel(spot_data)
            self.table_view.setModel(self.model)

        # we also update the canvas with the spot's data based on the selected channels
        # if nothing in the channels is highlighted, we don't plot anything

        if len(self.get_highlighted_channels()) != 0:
            highlighted_channels = self.get_highlighted_channels()
            print(highlighted_channels)
            # clear the canvas first
            # self.plot_canvas.clear()
            # for i, channel in enumerate(highlighted_channels):
            #     self.plot_canvas.scatter(spot_data['Time'], spot_data[channel], s=5, color='C'+str(i), alpha=0.6)
            #     self.plot_canvas.plot(spot_data['Time'], spot_data[channel], color='C'+str(i), label=channel)
            # self.plot_canvas.show_legend()
            
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
        highlightAction.triggered.connect(self.highlight_channel)

        contextMenu.exec_(self.channel_list.mapToGlobal(pos))

    def highlight_channel(self):
        # upon double clicking a channel, highlight the corresponding channel in the project if it was not already highlighted
        # if it was already highlighted, remove the highlight
        for item in self.channel_list.selectedItems():
            print(item.background().color().name())
            if item.background().color() == QColor(Qt.yellow):
                item.setBackground(QColor(Qt.white))
            else:
                item.setBackground(QColor(Qt.yellow))
            # then deselect the item
            item.setSelected(False)

    def load_data(self):
            global this_experiment
            global channels
            # if we are opening a new directory, we need to clear the previous data
            if this_experiment.shape[0] != 0:
                this_experiment = pd.DataFrame()
                channels = []
                self.channel_list.clear()
                self.spot_list.clear()
                self.table_view.setModel(PandasTableModel(pd.DataFrame()))
                # self.plot_canvas.clear()
                self.update_console('Cleared previous data')

            # make sure to only select folders
            options = QFileDialog.Options()
            directory = QFileDialog.getExistingDirectory(self, "Select Folder", "", options=options)
            try:
                this_experiment, channels = util.read_experiment_data(directory)
                self.update_console(f'Loaded data from {this_experiment.shape[0]} spots from {directory}')
                # update the channel list widget
                self.channel_list.clear()
                self.channel_list.addItems(channels)
                # update the spot list widget
                self.spot_list.clear()
                self.spot_list.addItems(this_experiment['spot_names'])
            except Exception as e:
                # print out the entire error message in the console
                self.update_console(f'Error loading data from {directory}: \n')
                exception_details = traceback.format_exception(type(e), e, e.__traceback__)
                for detail in exception_details:
                    self.update_console(detail)

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
