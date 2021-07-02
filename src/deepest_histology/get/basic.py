import random
import logging
from dataclasses import dataclass, fields
from typing import Iterable, Sequence, Iterator, List, Any, Union
from pathlib import Path
from numbers import Number

import torch
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn import preprocessing
from tqdm import tqdm

from ..experiment import Run, RunGetter
from ..utils import log_defaults

__all__ = ['Cohort', 'simple_run', 'multi_target']


PathLike = Union[str, Path]


class Cohort:
    """
    Args:
        tile_path:  Path of the directory the tiles are saved in.  For each
            slide, the tile path should contain a folder with the slide's name
            in which all of the slide's tiles are stored.
        clini_path:  The path to the clinical table, i.e. the table in which
            per-patient data is stored.
        slide_path:  The path to the slide table, i.e. the table assigning each
            slide to a patient.
    """
    def __init__(self, tile_path: PathLike, clini_path: PathLike, slide_path: PathLike) -> None:
        self.tile_path = Path(tile_path)
        self.tile_path = Path(tile_path)

        clini_path, slide_path = Path(clini_path), Path(slide_path)
        self.clini_df = (pd.read_csv(clini_path, dtype=str) if clini_path.suffix == '.csv'
                         else pd.read_excel(clini_path, dtype=str))
        self.slide_df = (pd.read_csv(slide_path, dtype=str) if slide_path.suffix == '.csv'
                         else pd.read_excel(slide_path, dtype=str))

        # TODO strip patient ids, slide names
        #clini_df['PATIENT'] = clini_df['PATIENT'].str.strip()
        #slide_df['PATIENT'] = slide_df['PATIENT'].str.strip()
        #slide_df['FILENAME'] = slide_df['FILENAME'].str.strip()


@log_defaults
def simple_run(
        project_dir: Path, /,
        target_label: str,
        train_cohorts: Iterable[Cohort] = [],
        test_cohorts: Iterable[Cohort] = [],
        max_tile_num: int = 250,
        seed: int = 0,
        valid_frac: float = .15,
        n_bins: int = 2,
        na_values: Iterable[Any] = [],
        min_support: int = 10) \
        -> Iterator[Run]:
    """Creates runs for a basic test-deploy procedure.

    This function will generate one training run per target label.  Due to large
    in-patient similarities in slides, only up to a fixed number of tiles will
    be sampled from each patient.  These runs will supply:

    -   A training set, if `train_cohorts` is not empty. The training set will
        be balanced in such a way that each class is represented with a number
        of tiles equal to that of the smallest class.
    -   A testing set, if `test_cohorts` is not empty.  The testing set may be
        unbalanced.

    If the target is continuous, it will be discretized.

    Args:
        project_dir:  Path to save project data to.
        train_cohorts:  The cohorts to use for training.
        test_cohorts:  The cohorts to test on.
        max_tile_num:  The maximum number of tiles to take from each patient.
        valid_frac:  The fraction of patients which will be reserved for
            validation during training.
        n_bins:  The number of bins to discretize continuous values into.
        na_values:  The class labels to consider as N/A values.
        min_support:  The minimum amount of class samples required for the class
            to be included in training.  Classes with less support are dropped.

    Yields:
        A single run to train and / or deploy a model on the given training and
        testing data.

    Example:
        To train a model:

        >>> get=partial(
        ...     simple_run
        ...     project_dir=Path('/path/to/result/dir'),
        ...     target_label='isMSIH',
        ...     train_cohort=Cohort(...),
        ...     max_tile_num=150,
        ...     na_values=['unavailable', 'inconclusive'])
    """
    logger = logging.getLogger()

    if train_cohorts:
        cohorts_df = _prepare_cohorts(
            train_cohorts, target_label, na_values, n_bins, min_support, logger)

        if cohorts_df[target_label].nunique() < 2:
            logger.warning(f'Not enough classes for target {target_label}! skipping...')
            return

        logger.info(f'Slide target counts: {dict(cohorts_df[target_label].value_counts())}')

        # split off validation set
        patients = cohorts_df.groupby('PATIENT')[target_label].first()
        _, valid_patients = train_test_split(
            patients.index, test_size=valid_frac, stratify=patients)
        cohorts_df['is_valid'] = cohorts_df['PATIENT'].isin(valid_patients)

        logger.info(f'Searching for training tiles')
        tiles_df = _get_tiles(
            cohorts_df=cohorts_df, max_tile_num=max_tile_num, seed=seed, logger=logger)

        logger.info(
            f'Training tiles: {dict(tiles_df[~tiles_df.is_valid][target_label].value_counts())}')
        logger.info(
            f'Validation tiles: {dict(tiles_df[tiles_df.is_valid][target_label].value_counts())}')
        valid_df = _balance_classes(tiles_df=tiles_df[tiles_df.is_valid], target=target_label)
        train_df = _balance_classes(tiles_df=tiles_df[~tiles_df.is_valid], target=target_label)
        logger.info(f'Training tiles after balancing: {len(train_df)}')
        logger.info(f'Validation tiles after balancing: {len(valid_df)}')

        train_df = pd.concat([train_df, valid_df])
    else:
        train_df = None

    # test set
    if (testing_set_path := project_dir/target_label/'testing_set.csv.zip').exists():
        # load old testing set if it exists
        logger.warning(f'{testing_set_path} already exists, using old testing set!')
        test_df = pd.read_csv(testing_set_path)
    elif test_cohorts:
        cohorts_df = _concat_cohorts(
            cohorts=test_cohorts, target_label=target_label, na_values=na_values, logger=logger)
        logger.info(f'Searching for training tiles')
        test_df = _get_tiles(
            cohorts_df=cohorts_df, max_tile_num=max_tile_num, seed=seed, logger=logger)
        logger.info(f'{len(test_df)} testing tiles: '
                    f'{dict(test_df[target_label].value_counts())}')
    else:
        test_df = None

    yield Run(directory=project_dir/target_label,
                target=target_label,
                train_df=train_df,
                test_df=test_df,
                logger=logger)


def _prepare_cohorts(
        cohorts: Iterable[Cohort], target_label: str, na_values: Iterable[str], n_bins: int,
        min_support: int, logger: logging.Logger) -> pd.DataFrame:
    """Preprocesses the cohorts.

    Discretizes continuous targets and drops classes for which only few examples
    are present.
    """
    cohorts_df = _concat_cohorts(
        cohorts=cohorts, target_label=target_label, na_values=na_values, logger=logger)

    # discretize values if necessary
    if cohorts_df[target_label].nunique() > 10:
        try:
            cohorts_df[target_label] = cohorts_df[target_label].map(float)
            logger.info(f'Discretizing {target_label}')
            cohorts_df[target_label] = _discretize(cohorts_df[target_label].values, n_bins=n_bins)
        except ValueError:
            pass

    # drop classes with insufficient support
    class_counts = cohorts_df[target_label].value_counts()
    rare_classes = (class_counts[class_counts < min_support]).index
    cohorts_df = cohorts_df[~cohorts_df[target_label].isin(rare_classes)]

    return cohorts_df


def _discretize(xs: Sequence[Number], n_bins: int) -> Sequence[str]:
    """Returns a discretized version of a Sequence of continuous values."""
    unsqueezed = torch.tensor(xs).reshape(-1, 1)
    est = preprocessing.KBinsDiscretizer(n_bins=n_bins, encode='ordinal').fit(unsqueezed)
    labels = [f'[-inf,{est.bin_edges_[0][1]})', # label for smallest class
                # labels for intermediate classes
                *(f'[{lower},{upper})'
                for lower, upper in zip(est.bin_edges_[0][1:], est.bin_edges_[0][2:-1])),
                f'[{est.bin_edges_[0][-2]}, inf)'] # label for largest class
    label_map = dict(enumerate(labels))
    discretized = est.transform(unsqueezed).reshape(-1).astype(int)
    return list(map(label_map.get, discretized)) # type: ignore


def _concat_cohorts(
        cohorts: Iterable[Cohort], target_label: str, na_values: Iterable[Any],
        logger: logging.Logger) \
        -> pd.DataFrame:
    """Constructs a dataframe containing patient, slide and label data for
    multiple cohorts.
    
    Returns:
        A dataframe with the columns 'PATIENT', `target` and 'BLOCK_DIR'.
    """

    cohort_dfs: List[pd.DataFrame] = []

    for cohort in cohorts:
        logger.info(f'For cohort {cohort.tile_path}')
        clini_df, slide_df, tile_path = cohort.clini_df, cohort.slide_df, cohort.tile_path

        if target_label not in clini_df:
            logger.warning(f'No column {target_label} in clini table for {tile_path}! Skipping cohort...')
            continue

        logger.info(f'#patients: {len(clini_df)}')
        logger.info(f'#slides: {len(slide_df)}')

        # only keep patients which have slides
        cohort_df = clini_df.merge(slide_df, on='PATIENT')
        logger.info(f'#slides after removing slides without patient data: {len(cohort_df)}')

        # filter n/a values
        cohort_df = cohort_df[cohort_df[target_label].notna()]
        for na_value in na_values:
            cohort_df = cohort_df[cohort_df[target_label] != na_value]

        logger.info(f'#slides after removing N/As: {len(cohort_df)}')

        # only keep slides which have tiles
        cohort_df['BLOCK_DIR'] = tile_path/cohort_df['FILENAME']

        cohort_dfs.append(cohort_df)

    # merge cohort dfs
    cohorts_df = cohort_dfs[0]
    for cohort_df in cohort_dfs[1:]:
        # check for patient overlap between cohorts
        if shared := set(cohorts_df['PATIENT']) & set(cohort_df['PATIENT']):
            raise RuntimeError(f'Patient overlap between cohorts', shared)

        cohorts_df = pd.concat([cohorts_df, cohort_df])

    return cohorts_df


def _get_tiles(
        cohorts_df: pd.DataFrame, max_tile_num: int, seed: int, logger: logging.Logger) \
        -> pd.DataFrame:
    """Create df containing patient, tiles, other data."""
    random.seed(seed)   #FIXME doesn't work
    tiles_dfs = []
    for patient, data in tqdm(cohorts_df.groupby('PATIENT')):
        tiles = [file
                 for tile_dir in data['BLOCK_DIR']
                 if tile_dir.exists()
                 for file in tile_dir.iterdir()]
        tiles = random.sample(tiles, min(len(tiles), max_tile_num))
        
        tiles_dfs.append(data.drop(columns='BLOCK_DIR')
                             .merge(pd.Series(tiles, name='tile_path'), how='cross'))

    tiles_df = pd.concat(tiles_dfs).reset_index(drop=True)
    logger.info(f'Found {len(tiles_df)} tiles for {len(tiles_df["PATIENT"].unique())} patients')

    return tiles_df


def _balance_classes(tiles_df: pd.DataFrame, target: str) -> pd.DataFrame:
    smallest_class_count = min(tiles_df[target].value_counts())
    for label in tiles_df[target].unique():
        tiles_with_label = tiles_df[tiles_df[target] == label]
        to_keep = tiles_with_label.sample(n=smallest_class_count).index
        tiles_df = tiles_df[(tiles_df[target] != label) | (tiles_df.index.isin(to_keep))]

    return tiles_df


def multi_target(get: RunGetter, project_dir: Path, target_labels: Iterable[str], *args, **kwargs) \
        -> Iterator[Run]:
    """Adapts a `RunGetter` into a multi-target one.

    Args:
        get:  The `RunGetter` to adapt; it has to take at least one keyword
            argument `target_label`.
        project_dir:  The directory to save the runs' results to.
        target_label:  The target labels to invoke `get` on.
        *args:  Additional arguments give to `get`.
        **kwargs:  Additional keyword arguments to give to `get`.

    Yields:
        The runs which would be yielded by `get` for each of the target labels,
        in the order of the target labels.  The run directories are prepended by
        a the name of the target label.

    Example:
        >>> from functools import partial
        >>> partial(
        ...     multi_target,
        ...     simple_run,     # `RunGetter` to modify
        ...     project_dir=Path('/path/to/project/dir'),
        ...     target_labels=['isMSIH', 'gender'],
        ...     # arguments to forward to `simple_run`
        ...     train_cohorts=[
        ...         Cohort(tile_path='/tile/path',
        ...                slide_path='/slide/path.csv',
        ...                clini_path='/clini/path.csv')],
        ...     na_values=['unknown'])
    """
    for target_label in target_labels:
        target_dir = project_dir/target_label
        target_dir.mkdir(parents=True, exist_ok=True)

        for run in get(*args, target_label=target_label, project_dir=target_dir, **kwargs): # type: ignore
            run.logger = logging.getLogger(f'{target_label}/{run.logger.name}')
            yield run