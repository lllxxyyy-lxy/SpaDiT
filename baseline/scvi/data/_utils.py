import logging
from typing import Tuple, Union

import anndata
import numba
import numpy as np
import pandas as pd
import scipy.sparse as sp_sparse

logger = logging.getLogger(__name__)


def _compute_library_size(
    data: Union[sp_sparse.spmatrix, np.ndarray]
) -> Tuple[np.ndarray, np.ndarray]:
    sum_counts = data.sum(axis=1)
    masked_log_sum = np.ma.log(sum_counts)
    if np.ma.is_masked(masked_log_sum):
        logger.warning(
            "This dataset has some empty cells, this might fail inference."
            "Data should be filtered with `scanpy.pp.filter_cells()`"
        )
    log_counts = masked_log_sum.filled(0)
    local_mean = (np.mean(log_counts).reshape(-1, 1)).astype(np.float32)
    local_var = (np.var(log_counts).reshape(-1, 1)).astype(np.float32)
    return local_mean, local_var


def _compute_library_size_batch(
    adata,
    batch_key: str,
    local_l_mean_key: str = None,
    local_l_var_key: str = None,
    layer=None,
    copy: bool = False,
):
    """
    Computes the library size.

    Parameters
    ----------
    adata
        anndata object containing counts
    batch_key
        key in obs for batch information
    local_l_mean_key
        key in obs to save the local log mean
    local_l_var_key
        key in obs to save the local log variance
    layer
        if not None, will use this in adata.layers[] for X
    copy
        if True, returns a copy of the adata

    Returns
    -------
    type
        anndata.AnnData if copy was True, else None

    """
    if batch_key not in adata.obs_keys():
        raise ValueError("batch_key not valid key in obs dataframe")
    local_means = np.zeros((adata.shape[0], 1))
    local_vars = np.zeros((adata.shape[0], 1))
    batch_indices = adata.obs[batch_key]
    for i_batch in np.unique(batch_indices):
        idx_batch = np.squeeze(batch_indices == i_batch)
        if layer is not None:
            if layer not in adata.layers.keys():
                raise ValueError("layer not a valid key for adata.layers")
            data = adata[idx_batch].layers[layer]
        else:
            data = adata[idx_batch].X
        (local_means[idx_batch], local_vars[idx_batch]) = _compute_library_size(data)
    if local_l_mean_key is None:
        local_l_mean_key = "_scvi_local_l_mean"
    if local_l_var_key is None:
        local_l_var_key = "_scvi_local_l_var"

    if copy:
        copy = adata.copy()
        copy.obs[local_l_mean_key] = local_means
        copy.obs[local_l_var_key] = local_vars
        return copy
    else:
        adata.obs[local_l_mean_key] = local_means
        adata.obs[local_l_var_key] = local_vars


def _check_nonnegative_integers(
    data: Union[pd.DataFrame, np.ndarray, sp_sparse.spmatrix]
):
    """Approximately checks values of data to ensure it is count data."""
    if isinstance(data, np.ndarray):
        data = data
    elif issubclass(type(data), sp_sparse.spmatrix):
        data = data.data
    elif isinstance(data, pd.DataFrame):
        data = data.to_numpy()
    else:
        raise TypeError("data type not understood")

    check = data[:10]
    return _check_is_counts(check)


@numba.njit(cache=True)
def _check_is_counts(data):
    for d in data.flat:
        if d < 0 or d % 1 != 0:
            return False
    return True


def _get_batch_mask_protein_data(
    adata: anndata.AnnData, protein_expression_obsm_key: str, batch_key: str
):
    """
    Returns a list with length number of batches where each entry is a mask.

    The mask is over cell measurement columns that are present (observed)
    in each batch. Absence is defined by all 0 for that protein in that batch.
    """
    pro_exp = adata.obsm[protein_expression_obsm_key]
    pro_exp = pro_exp.to_numpy() if isinstance(pro_exp, pd.DataFrame) else pro_exp
    batches = adata.obs[batch_key].values
    batch_mask = {}
    for b in np.unique(batches):
        b_inds = np.where(batches.ravel() == b)[0]
        batch_sum = pro_exp[b_inds, :].sum(axis=0)
        all_zero = batch_sum == 0
        batch_mask[b] = ~all_zero

    return batch_mask


def _check_anndata_setup_equivalence(adata_source, adata_target):
    """Checks if target setup is equivalent to source."""
    if isinstance(adata_source, anndata.AnnData):
        _scvi_dict = adata_source.uns["_scvi"]
    else:
        _scvi_dict = adata_source
    adata = adata_target

    stats = _scvi_dict["summary_stats"]

    target_n_vars = adata.shape[1]
    error_msg = (
        "Number of {} in anndata different from initial anndata used for training."
    )
    if target_n_vars != stats["n_vars"]:
        raise ValueError(error_msg.format("vars"))

    error_msg = (
        "There are more {} categories in the data than were originally registered. "
        + "Please check your {} categories as well as adata.uns['_scvi']['categorical_mappings']."
    )
    self_categoricals = _scvi_dict["categorical_mappings"]
    self_batch_mapping = self_categoricals["_scvi_batch"]["mapping"]

    adata_categoricals = adata.uns["_scvi"]["categorical_mappings"]
    adata_batch_mapping = adata_categoricals["_scvi_batch"]["mapping"]
    # check if the categories are the same
    error_msg = (
        "Categorial encoding for {} is not the same between "
        + "the anndata used to train the model and the anndata just passed in. "
        + "Categorical encoding needs to be same elements, same order, and same datatype.\n"
        + "Expected categories: {}. Received categories: {}.\n"
        + "Try running `dataset.transfer_anndata_setup()` or deleting `adata.uns['_scvi']."
    )
    if not _assert_equal_mapping(self_batch_mapping, adata_batch_mapping):
        raise ValueError(
            error_msg.format("batch", self_batch_mapping, adata_batch_mapping)
        )
    self_labels_mapping = self_categoricals["_scvi_labels"]["mapping"]
    adata_labels_mapping = adata_categoricals["_scvi_labels"]["mapping"]
    if not _assert_equal_mapping(self_labels_mapping, adata_labels_mapping):
        raise ValueError(
            error_msg.format("label", self_labels_mapping, adata_labels_mapping)
        )

    # validate any extra categoricals
    if "extra_categorical_mappings" in _scvi_dict.keys():
        target_extra_cat_maps = adata.uns["_scvi"]["extra_categorical_mappings"]
        for key, val in _scvi_dict["extra_categorical_mappings"].items():
            target_map = target_extra_cat_maps[key]
            if not _assert_equal_mapping(val, target_map):
                raise ValueError(error_msg.format(key, val, target_map))
    # validate any extra continuous covs
    if "extra_continuous_keys" in _scvi_dict.keys():
        if "extra_continuous_keys" not in adata.uns["_scvi"].keys():
            raise ValueError('extra_continuous_keys not in adata.uns["_scvi"]')
        target_cont_keys = adata.uns["_scvi"]["extra_continuous_keys"]
        if not _scvi_dict["extra_continuous_keys"].equals(target_cont_keys):
            raise ValueError(
                "extra_continous_keys are not the same between source and target"
            )


def _assert_equal_mapping(mapping1, mapping2):

    return pd.Index(mapping1).equals(pd.Index(mapping2))
