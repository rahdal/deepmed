import random
import logging
from typing import Iterable, Iterator, Any
from pathlib import Path

import pandas as pd
from sklearn.model_selection import StratifiedKFold

from ..experiment import Run
from ..basic.get_runs import _prepare_cohorts, _get_tiles, _balance_classes, Cohort
from ..utils import log_defaults

__all__ = ['get_crossval_runs']


logger = logging.getLogger(__name__)


@log_defaults
def get_crossval_runs(
        project_dir: Path,
        target_label: str,
        cohorts: Iterable[Cohort] = [],
        max_tile_num: int = 500,
        folds: int = 3,
        seed: int = 0,
        valid_frac: float = .1,
        n_bins: int = 2,
        na_values: Iterable[Any] = [],
        min_support: int = 10,
        **kwargs) -> Iterator[Run]:

    existing_fold_dirs = [fold_dir
                            for fold_dir in (project_dir/target_label).iterdir()
                            if fold_dir.is_dir() and fold_dir.name.startswith('fold_')] \
            if (project_dir/target_label).is_dir() else None

    logger.info(f'For target {target_label}:')
    if existing_fold_dirs:
        logger.info(f'Using old training and testing set')
        for fold_dir in existing_fold_dirs:
            train_path = fold_dir/'training_set.csv.zip'
            test_path = fold_dir/'testing_set.csv.zip'
            train_df = (pd.read_csv(train_path)
                        if train_path.exists() and not (fold_dir/'export.pkl').exists()
                        else None)
            test_df = (pd.read_csv(test_path)
                        if test_path.exists() and not (fold_dir/'predictions.csv.zip').exists()
                        else None)

            yield Run(directory=fold_dir,
                        target=target_label,
                        train_df=train_df,
                        test_df=test_df)
    else:
        assert cohorts, 'No old training and testing sets found and no cohorts given!'
        cohorts_df = _prepare_cohorts(
            cohorts, target_label, na_values, n_bins, min_support)

        if cohorts_df[target_label].nunique() < 2:
            logger.warning(f'Not enough classes for target {target_label}! skipping...')
            return

        logger.info(f'Slide target counts: {dict(cohorts_df[target_label].value_counts())}')

        folded_df = _create_folds(cohorts_df=cohorts_df, target_label=target_label, folds=folds,
                                valid_frac=valid_frac, seed=seed)
        logger.info(f'Searching for tiles')
        tiles_df = _get_tiles(cohorts_df=folded_df, max_tile_num=max_tile_num,
                            target=target_label, seed=seed)

        fold_runs = []
        for fold in sorted(folded_df.fold.unique()):
            logger.info(f'For fold {fold}:')
            logger.info(f'Training tiles: {dict(tiles_df[(tiles_df.fold != fold) & ~tiles_df.is_valid][target_label].value_counts())}')
            logger.info(f'Validation tiles: {dict(tiles_df[(tiles_df.fold != fold) & tiles_df.is_valid][target_label].value_counts())}')
            train_df = _balance_classes(
                tiles_df=tiles_df[(tiles_df.fold != fold) & ~tiles_df.is_valid],
                target=target_label)
            valid_df = _balance_classes(
                tiles_df=tiles_df[(tiles_df.fold != fold) & tiles_df.is_valid],
                target=target_label)
            logger.info(f'{len(train_df)} training tiles')
            logger.info(f'{len(valid_df)} validation tiles')

            test_df = tiles_df[tiles_df.fold == fold]
            logger.info(f'{len(test_df)} testing tiles')
            assert not test_df.empty, 'Empty fold in cross validation!'

            run = Run(
                directory=project_dir/target_label/f'fold_{fold}',
                target=target_label,
                train_df=pd.concat([train_df, valid_df]),
                test_df=test_df)

            _save_run_files(run)
            fold_runs.append(run)

        for run in fold_runs:
            yield(run)


def _save_run_files(run: Run) -> None:
    logger.info(f'Saving training/testing data for run {run.directory}...')
    run.directory.mkdir(exist_ok=True, parents=True)
    if run.train_df is not None and \
            not (training_set_path := run.directory/'training_set.csv.zip').exists():
        run.train_df.to_csv(training_set_path, index=False, compression='zip')
    if run.test_df is not None and \
            not (testing_set_path := run.directory/'testing_set.csv.zip').exists():
        run.test_df.to_csv(testing_set_path, index=False, compression='zip')


# TODO define types for dfs
def _create_folds(
        cohorts_df: pd.DataFrame, target_label, folds: int, valid_frac: float, seed: int) \
        -> pd.DataFrame:

    kf = StratifiedKFold(n_splits=folds, random_state=seed, shuffle=True)

    # Pepare our dataframe
    # We enumerate each fold; this way, the training set for the `k`th iteration can be easily
    # obtained through `df[df.fold != k]`. Additionally, we sample a validation set for early
    # stopping frotarget_labell.
    cohorts_df['fold'] = 0
    cohorts_df['is_valid'] = False
    for fold, (train_idx, test_idx) \
            in enumerate(kf.split(cohorts_df['PATIENT'], cohorts_df[target_label])):
        #FIXME: remove ugly iloc magic to prevent `SettingWithCopyWarning`
        cohorts_df.iloc[test_idx, cohorts_df.columns.get_loc('fold')] = fold
        test_df = cohorts_df.iloc[test_idx]
        cohorts_df.is_valid |= \
            cohorts_df.index.isin(test_df.sample(frac=valid_frac, random_state=seed).index)

    return cohorts_df
