import sys
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QPushButton, QLabel, QFileDialog, QScrollArea, QMenu, QListWidget, QAbstractItemView
from PyQt5.QtCore import Qt, QPoint
from PyQt5.QtGui import QFont, QColor
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.figure import Figure
from matplotlib.widgets import RectangleSelector
import pandas as pd
import os
import traceback

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

class PlotCanvas(FigureCanvas):
    def __init__(self, parent=None, width=5, height=4, dpi=100):
        fig = Figure(figsize=(width, height), dpi=dpi)
        self.axes = fig.add_subplot(111)
        super().__init__(fig)
        self.setParent(parent)

    def plot(self, data):
        self.axes.clear()
        self.axes.plot(data['x'], data['y'], 'r-')
        self.draw()

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("LA-ICP-MS Data Visualization")
        self.setGeometry(100, 100, 800, 600)

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

        self.plot_canvas = PlotCanvas(self, width=8, height=6)

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




        # now add a scrollable list widget for displaying the spot names
        self.spot_list_label = QLabel("Spots")
        self.spot_list_label.setFont(QFont("Dejavu Sans", 12, QFont.Bold))
        self.spot_list = QListWidget()

        # put the channel list and spot list in a vertical layout
        self.v_layout = QVBoxLayout()
        self.v_layout.addWidget(self.channel_list_label)
        self.v_layout.addWidget(self.channel_list)
        self.v_layout.addWidget(self.spot_list_label)
        self.v_layout.addWidget(self.spot_list)

        # put the vertical layout and the canvas in a horizontal layout
        self.h_layout = QHBoxLayout()
        self.h_layout.addLayout(self.v_layout)
        self.h_layout.addWidget(self.plot_canvas)

        self.layout.addLayout(self.h_layout)
 
        self.layout.addWidget(self.load_button)
        self.layout.addWidget(self.scroll_area)


    def show_context_menu(self, pos: QPoint):
        contextMenu = QMenu(self)

        highlightAction = contextMenu.addAction("Highlight Selected")
        highlightAction.triggered.connect(self.highlight_channel)

        contextMenu.exec_(self.channel_list.mapToGlobal(pos))

    def highlight_channel(self):
        # upon double clicking a channel, highlight the corresponding channel in the project if it was not already highlighted
        # if it was already highlighted, remove the highlight
        for item in self.channel_list.selectedItems():
            if item.background().color() == QColor(Qt.yellow):
                item.setBackground(QColor(Qt.white))
            else:
                item.setBackground(QColor(Qt.yellow))
            # then deselect the item
            item.setSelected(False)

    def load_data(self):
            # make sure to only select folders
            options = QFileDialog.Options()
            directory = QFileDialog.getExistingDirectory(self, "Select Folder", "", options=options)
            try:
                this_experiment, channels = read_experiment_data(directory)
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

def read_experiment_data(directory):
    '''
    function for extracting spot names from files exported from Thermo iCap

    You may follow the instruction on Iolite website below for guidance on generating the proper files from iCap
    https://iolite.xyz/docs/import_prep/exporting_data_from_instruments.html

    Parameters
    ----------
    directory : str
        path to the folder containing the files
        we assume that the files in the folders are in .csv format
    
    Returns
    -------
    spot_names : list
        list of spot names extracted from the files
    spot_paths : list
        list of paths to the files
    '''
    spot_names = []
    spot_paths = []
    # use the first .csv file to get the channel names
    first_file = open(os.path.join(directory,[f if f.endswith('.csv') else None for f in os.listdir(directory)][0])).readlines()

    # now update the global variable channels
    channels = first_file[13].split(',')[1:]

    # loop through all the .csv files in the directory using io and get their first lines
    for file in os.listdir(directory):
        if file.endswith('.csv'):
            this_path = os.path.join(directory, file)
            spot_paths.append(this_path)
            file = open(this_path)
            lines = file.readlines()
            spot_name = lines[0].split(':')[0]
            spot_names.append(spot_name)
            file.close()

    # create a pandas dataframe to store the experiment data
    experiment = pd.DataFrame()
    experiment['spot_names'] = spot_names
    experiment['spot_paths'] = spot_paths
    
    experiment = experiment.sort_values(by='spot_names').reset_index(drop=True)
    return experiment, channels


def main():
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    sys.exit(app.exec_())

if __name__ == "__main__":
    main()
