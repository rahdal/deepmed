import logging

logging.basicConfig(level=logging.DEBUG)
#FIXME this is a rather hacky way to set the global stderr handler log level
logging.getLogger().handlers[0].setLevel(logging.INFO)

from pathlib import Path
from deepest_histology.experiment import do_experiment
from deepest_histology.get import *
from deepest_histology.metrics import *
from functools import partial
