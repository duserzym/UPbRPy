from PyQt5.QtWidgets import QListWidget, QAbstractItemView, QApplication
from PyQt5.QtCore import Qt, QVariant
from PyQt5.QtWebEngineWidgets import QWebEngineView
from PyQt5.QtCore import QAbstractTableModel
import plotly.graph_objects as go
import plotly.io as pio

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

    def add_line(self, x, y, data, color, mode='lines', **kwargs):
        self.fig.add_trace(go.Scatter
            (x=data[x], y=data[y], mode=mode, name=y, line=dict(color=color), **kwargs))
        
        self.auto_range(data)
        self.display_plot()

    def remove_line(self, line_id):
        # remove the line with the name of line_id
        self.fig.data = [line for line in self.fig.data if line.name != line_id]

        self.display_plot()

    def draw_selection(self, start_time, end_time, mean, std2, mean_color, std_color, selection_type='baseline'):
        '''
        function for drawing the selection on the plot

        Parameters
        ----------
        start_time : datetime
            start time of the selection
        end_time : datetime
            end time of the selection
        mean : float
            mean of the selection
        std2 : float
            2 standard deviations of the selection
        mean_color : str 
            color of the selected mean
        std_color : str
            color of the standard deviation region
        selection_type : str
            type of the selection, either 'baseline' or 'plateau'
        '''
        # print(f'Drawing selection from {start_time} to {end_time} with mean {mean} and 2 std {std2}')
        assert selection_type in ['baseline', 'plateau'], 'selection_type must be either "baseline" or "plateau"'
        if selection_type == 'baseline':
            mean_name = 'baseline mean'
            std_name = 'baseline std'
            mean_color = 'red'
            std_color = 'lightcoral'
        else:
            mean_name = 'plateau mean'
            std_name = 'plateau std'
            mean_color = 'blue'
            std_color = 'lightblue'
        # draw the selection on the plot
        self.fig.add_trace(go.Scatter(x=[start_time, end_time], y=[mean, mean], mode='lines', name=mean_name, line=dict(color=mean_color)))
        self.fig.add_shape(type="rect",
                            x0=start_time,
                            y0=mean-std2,   
                            x1=end_time,
                            y1=mean+std2,
                            line=dict(color=std_color),
                            fillcolor=std_color,
                            opacity=0.5,
                            layer="above", name=std_name)
        self.display_plot()
    
    def remove_selection(self):
        # remove the selection from the plot
        self.fig.data = [line for line in self.fig.data if line.name not in ['baseline mean', 'plateau mean']]
        self.fig.layout.shapes = [shape for shape in self.fig.layout.shapes if shape.name not in ['baseline std', 'plateau std']]

        self.display_plot()

    def auto_range(self, total_data, highlighted_channels=[], selected_spot=None):
        if selected_spot == None:
            self.fig.update_xaxes(range=[total_data['Time'].min(), 
                                         total_data['Time'].max()])
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