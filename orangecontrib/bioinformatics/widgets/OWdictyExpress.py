""" dictyExpress widget """
import sys
import os
import threading
import requests_cache


from requests.exceptions import ConnectionError

from AnyQt.QtCore import Qt, QThreadPool, QSize
from AnyQt.QtGui import QFont
from AnyQt.QtWidgets import QTreeWidget, QTreeWidgetItem, QLabel, QLineEdit, QApplication, QFrame

from Orange.data import Table
from Orange.widgets import gui, settings
from Orange.widgets.widget import OWWidget, Msg
from Orange.widgets.utils.signals import Output
# from Orange.widgets.utils.datacaching import data_hints

from orangecontrib.bioinformatics import resolwe
from orangecontrib.bioinformatics.utils import local_cache
from orangecontrib.bioinformatics.resolwe.utils import etc_to_table
from orangecontrib.bioinformatics.widgets.utils.data import TAX_ID, GENE_AS_ATTRIBUTE_NAME
from orangecontrib.bioinformatics.widgets.utils.concurrent import Worker
from orangecontrib.bioinformatics.resolwe.utils import response_to_json


Labels = [
    (" ", " "),
    ("var.project", "Project"),
    ("static.name", "Experiment"),
    ("static.cite", "Citation"),
    ("var.growth", "Growth"),
    ("var.treatment", "Treatment"),
    ("var.strain", "Parental strain")]


# Creates line separator
def h_line():
    line = QFrame()
    line.setFrameShape(QFrame.HLine)
    line.setFrameShadow(QFrame.Sunken)
    return line


#  Support cache with requests_cache module
cache_path = os.path.join(local_cache, "resolwe")

try:
    os.makedirs(cache_path)
except OSError:
    pass

cache_file = os.path.join(cache_path, 'dictyExpress_cache')
requests_cache.install_cache(cache_name=cache_file, backend='sqlite')


def get_cached_ids():
    cached_object = requests_cache.core.get_cache()
    responses = [cached_object.get_response_and_time(response) for response in cached_object.responses]
    responses_json = [response_to_json(response) for response, _ in responses]
    return [response['id'] for response in responses_json if 'id' in response]


class OWdictyExpress(OWWidget):

    name = "dictyExpress"
    description = "Time-course gene expression data"
    icon = "../widgets/icons/OWdictyExpress.png"
    want_main_area = True
    priority = 3

    class Inputs:
        pass

    class Outputs:
        etc_data = Output("Data", Table)

    class Error(OWWidget.Error):
        unreachable_host = Msg('Host not reachable')
        invalid_credentials = Msg('Invalid credentials')

    username = settings.Setting('')
    password = settings.Setting('')
    setTimeVariable = settings.Setting(False)
    gene_as_attr_name = settings.Setting(0)

    def __init__(self):
        super().__init__()

        self.res = None
        self.orgnism = 352472
        self.server = 'https://dictyexpress.research.bcm.edu'
        self.headerLabels = [x[1] for x in Labels]
        self.searchString = ""
        self.items = []
        # self.lastSelected = None  # store last selected customTreeItem

        self.progress_bar = None
        # threads
        self.threadpool = QThreadPool()

        # Login Section

        box = gui.widgetBox(self.controlArea, 'Login')

        self.namefield = gui.lineEdit(box, self, "username", "Username:", labelWidth=100, orientation='horizontal',
                                      callback=self.auth_changed)

        self.passfield = gui.lineEdit(box, self, "password", "Password:", labelWidth=100, orientation='horizontal',
                                      callback=self.auth_changed)

        self.passfield.setEchoMode(QLineEdit.Password)

        self.controlArea.layout().addWidget(h_line())

        box = gui.widgetBox(self.controlArea, "Output", addSpace=True)
        gui.radioButtonsInBox(box, self, "gene_as_attr_name", ["Genes in rows", "Genes in columns"],
                              callback=self.commit)

        self.controlArea.layout().addWidget(h_line())

        self.commit_button = gui.button(self.controlArea, self, "Commit", callback=self.commit)
        self.handle_commit_button(False)

        self.refresh_button = gui.button(self.controlArea, self, "Refresh", callback=self.refresh)
        self.handle_cache_button(True)

        gui.rubber(self.controlArea)

        # Experiment Section

        label = QLabel("Available projects:")
        my_font = QFont()
        my_font.setBold(True)
        label.setFont(my_font)
        self.mainArea.layout().addWidget(label)

        self.mainArea.layout().addWidget(h_line())

        self.filter = gui.lineEdit(self.mainArea, self, "searchString", "Filter:", callbackOnType=True,
                                   callback=self.search_update)

        self.experimentsWidget = QTreeWidget(alternatingRowColors=True,
                                             rootIsDecorated=False,
                                             uniformRowHeights=True,
                                             sortingEnabled=True)

        self.experimentsWidget.setItemDelegateForColumn(
            0, gui.IndicatorItemDelegate(self, role=Qt.DisplayRole))

        self.experimentsWidget.selectionModel().selectionChanged.connect(
            self.onSelectionChanged)

        self.experimentsWidget.setHeaderLabels(self.headerLabels)
        self.mainArea.layout().addWidget(self.experimentsWidget)

        self.auth_set()
        self.connect()
        self.sizeHint()

    def sizeHint(self):
        return QSize(1400, 680)

    def auth_set(self):
        self.passfield.setDisabled(not self.username)

    def auth_changed(self):
        self.auth_set()
        self.connect()

    def refresh(self):
        self.reset()
        self.load_experiments()

    def reset(self):
        self.experimentsWidget.clear()  # clear QTreeWidget
        self.items = []
        # self.lastSelected = None
        self.searchString = ""
        self.handle_commit_button(False)

    def search_update(self):
        parts = self.searchString.split()
        for item in self.items:
            item.setHidden(not all(s in item for s in parts))

    def progress_advance(self):
        # GUI should be updated in main thread. That's why we are calling advance method here
        assert threading.current_thread() == threading.main_thread()
        if self.progress_bar:
            self.progress_bar.advance()

    def handle_error(self, ex):
        self.progress_bar.finish()
        self.setStatusMessage('')
        if isinstance(ex, ConnectionError) or isinstance(ex, ValueError):
            self.Error.unreachable_host()

        print(ex)

    def load_experiments_result(self, experiments):
        self.load_tree_items(experiments)
        self.progress_bar.finish()
        self.setStatusMessage('')

    def connect(self):
        self.res = None
        self.Error.clear()
        self.reset()
        self.handle_commit_button(False)
        self.handle_cache_button(False)

        user, password = resolwe.DEFAULT_EMAIL, resolwe.DEFAULT_PASSWD
        if self.username or self.password:
            user, password = self.username, self.password

        try:
            self.res = resolwe.connect(user, password, self.server, 'genesis')
        except resolwe.ResolweAuthException as e:
            self.Error.invalid_credentials()
        else:
            self.load_experiments()
            self.handle_cache_button(True)

    def load_experiments(self):
        if self.res:
            # init progress bar
            self.progress_bar = gui.ProgressBar(self, iterations=2)
            # status message
            self.setStatusMessage('downloading experiments')

            worker = Worker(self.res.fetch_etc_objects, progress_callback=True)
            worker.signals.progress.connect(self.progress_advance)
            worker.signals.result.connect(self.load_experiments_result)
            worker.signals.error.connect(self.handle_error)

            # move download process to worker thread
            self.threadpool.start(worker)

    def load_tree_items(self, list_of_exp):
        self.items = [CustomTreeItem(self.experimentsWidget, item) for item in list_of_exp]

        for i in range(len(self.headerLabels)):
            self.experimentsWidget.resizeColumnToContents(i)

        self.set_cached_indicator()

    def onSelectionChanged(self):
        self.handle_commit_button(True)

    def handle_commit_button(self, handle):
        self.commit_button.setEnabled(handle)

    def handle_cache_button(self, handle):
        self.refresh_button.setEnabled(handle)

    def send_to_output(self, result):
        self.progress_bar.finish()
        self.setStatusMessage('')

        etc_json, table_name = result

        # convert to table
        data = etc_to_table(etc_json, bool(self.gene_as_attr_name))
        # set table name
        data.name = table_name
        # add table attributes
        data.attributes[TAX_ID] = self.orgnism
        data.attributes[GENE_AS_ATTRIBUTE_NAME] = bool(self.gene_as_attr_name)
        # reset cache indicators
        self.set_cached_indicator()
        # send data to the output signal
        self.Outputs.etc_data.send(data)

    def commit(self):
        self.Error.clear()

        selected_item = self.experimentsWidget.currentItem()  # get selected TreeItem

        if selected_item:
            # init progress bar
            self.progress_bar = gui.ProgressBar(self, iterations=1)
            # status message
            self.setStatusMessage('downloading experiment data')

            worker = Worker(self.res.download_etc_data,
                            selected_item.gen_data_id,
                            table_name=selected_item.data_name,
                            progress_callback=True)

            worker.signals.progress.connect(self.progress_advance)
            worker.signals.result.connect(self.send_to_output)
            worker.signals.error.connect(self.handle_error)

            # move download process to worker thread
            self.threadpool.start(worker)

    def set_cached_indicator(self):
        cached = get_cached_ids()
        for item in self.items:

            if item.gen_data_id in cached:
                item.setData(0, Qt.DisplayRole, " ")
            else:
                item.setData(0, Qt.DisplayRole, "")


class CustomTreeItem(QTreeWidgetItem):

    def __init__(self, parent, gen_data):
        super(CustomTreeItem, self).__init__(parent)  # Init super class (QtGui.QTreeWidgetItem )

        self._gen_data = gen_data  # GenData object
        self.set_rows(self._gen_data.annotation)  # set rows in QTreeWidget

    def __contains__(self, text):
        return any(text.upper() in str(self.text(i)).upper() for i in range(self.columnCount()))

    @property
    def gen_data_id(self):
        return self._gen_data.id

    @property
    def data_name(self):
        try:
            project = self._gen_data.var['project']
            experiment = self._gen_data.static['name']
        except AttributeError:
            project = ''
            experiment = ''

        return '{} ({})'.format(project, experiment)

    def set_rows(self, row):
        for index, label in enumerate(Labels):
            if index > 0:
                try:
                    if type(row[label[0]]["value"]) == list:
                        self.setText(index, row[label[0]]["value"][0]["name"])
                    else:
                        self.setText(index, row[label[0]]["value"])
                except IndexError:
                    self.setText(index, 'No data')


if __name__ == "__main__":

    def test_main():
        app = QApplication(sys.argv)
        w = OWdictyExpress()
        w.show()
        r = app.exec_()
        w.saveSettings()
        return r

    sys.exit(test_main())
