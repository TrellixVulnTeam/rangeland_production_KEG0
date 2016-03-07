# -*- coding: utf-8 -*-
"""Coastal Blue Carbon Model."""

import csv
import os
import pprint as pp
import shutil
import logging
import math
import time
import itertools

import numpy as np
from osgeo import gdal
import pygeoprocessing.geoprocessing as geoprocess

from .. import utils as invest_utils

# using largest negative 32-bit floating point number
# reasons: practical limit for 32 bit floating point and most outputs should
#          be positive
NODATA_FLOAT = -16777216

logging.basicConfig(format='%(asctime)s %(name)-20s %(levelname)-8s \
%(message)s', level=logging.DEBUG, datefmt='%m/%d/%Y %H:%M:%S ')
LOGGER = logging.getLogger(
    'natcap.invest.coastal_blue_carbon.coastal_blue_carbon')


def execute(args):
    """Entry point for Coastal Blue Carbon model.

    Args:
        workspace_dir (str): location into which all intermediate and
            output files should be placed.
        results_suffix (str): a string to append to output filenames.
        lulc_lookup_uri (str): filepath to a CSV table used to convert
            the lulc code to a name. Also used to determine if a given lulc
            type is a coastal blue carbon habitat.
        lulc_transition_matrix_uri (str): generated by the preprocessor. This
            file must be edited before it can be used by the main model. The
            left-most column represents the source lulc class, and the top row
            represents the destination lulc class.
        carbon_pool_initial_uri (str): the provided CSV table contains
            information related to the initial conditions of the carbon stock
            within each of the three pools of a habitat. Biomass includes
            carbon stored above and below ground. All non-coastal blue carbon
            habitat lulc classes are assumed to contain no carbon. The values
            for 'biomass', 'soil', and 'litter' should be given in terms of
            Megatonnes CO2 e/ ha.
        carbon_pool_transient_uri (str): the provided CSV table contains
            information related to the transition of carbon into and out of
            coastal blue carbon pools. All non-coastal blue carbon habitat lulc
            classes are assumed to neither sequester nor emit carbon as a
            result of change. The 'yearly_accumulation' values should be given
            in terms of Megatonnes of CO2 e/ha-yr. The 'half-life' values must
            be given in terms of years. The 'disturbance' values must be given
            as a decimal (e.g. 0.5 for 50%) of stock distrubed given a transition
            occurs away from a lulc-class.
        lulc_baseline_map_uri (str): a GDAL-supported raster representing the
            baseline landscape/seascape.
        lulc_transition_maps_list (list): a list of GDAL-supported rasters
            representing the landscape/seascape at particular points in time.
            Provided in chronological order.
        lulc_transition_years_list (list): a list of years that respectively
            correspond to transition years of the rasters. Provided in
            chronological order.
        analysis_year (int): optional. Indicates how many timesteps to run the
            transient analysis beyond the last transition year. Must come
            chronologically after the last transition year if provided.
            Otherwise, the final timestep of the model will be set to the last
            transition year.
        do_economic_analysis (bool): boolean value indicating whether model
            should run economic analysis.
        do_price_table (bool): boolean value indicating whether a price table
            is included in the arguments and to be used or a price and interest
            rate is provided and to be used instead.
        price (float): the price per Megatonne CO2 e at the base year.
        interest_rate (float): the interest rate on the price per Megatonne
            CO2e, compounded yearly.  Provided as a percentage (e.g. 3.0 for
            3%).
        price_table_uri (bool): if `args['do_price_table']` is set to `True`
            the provided CSV table is used in place of the initial price and
            interest rate inputs. The table contains the price per Megatonne
            CO2e sequestered for a given year, for all years from the original
            snapshot to the analysis year, if provided.
        discount_rate (float): the discount rate on future valuations of
            sequestered carbon, compounded yearly.  Provided as a percentage
            (e.g. 3.0 for 3%).

    Example Args::

        args = {
            'workspace_dir': 'path/to/workspace/',
            'results_suffix': '',
            'lulc_lookup_uri': 'path/to/lulc_lookup_uri',
            'lulc_transition_matrix_uri': 'path/to/lulc_transition_uri',
            'carbon_pool_initial_uri': 'path/to/carbon_pool_initial_uri',
            'carbon_pool_transient_uri': 'path/to/carbon_pool_transient_uri',
            'lulc_baseline_map_uri': 'path/to/baseline_map.tif',
            'lulc_transition_maps_list': [raster1_uri, raster2_uri, ...],
            'lulc_transition_years_list': [2000, 2005, ...],
            'analysis_year': 2100,
            'do_economic_analysis': '<boolean>',
            'do_price_table': '<boolean>',
            'price': '<float>',
            'interest_rate': '<float>',
            'price_table_uri': 'path/to/price_table',
            'discount_rate': '<float>'
        }
    """
    LOGGER.info("Starting Coastal Blue Carbon model run...")
    d = get_inputs(args)

    # Setup Logging
    num_blocks = get_num_blocks(d['C_prior_raster'])
    current_time = time.time()

    block_iterator = enumerate(geoprocess.iterblocks(d['C_prior_raster']))
    C_nodata = geoprocess.get_nodata_from_uri(d['C_prior_raster'])

    for block_idx, (offset_dict, C_prior) in block_iterator:
        # Update User
        if time.time() - current_time >= 2.0:
            LOGGER.info("Processing block %i of %i" %
                        (block_idx+1, num_blocks))
            current_time = time.time()

        # Initialization
        timesteps = d['timesteps']

        x_size, y_size = C_prior.shape

        # timesteps+1 to include initial conditions
        stock_shape = (timesteps+1, x_size, y_size)
        S_biomass = np.zeros(stock_shape, dtype=np.float32)  # Stock
        S_soil = np.zeros(stock_shape, dtype=np.float32)
        S_litter = np.zeros(stock_shape, dtype=np.float32)
        T = np.zeros(stock_shape, dtype=np.float32)  # Total Carbon Stock

        timestep_shape = (timesteps, x_size, y_size)
        A_biomass = np.zeros(timestep_shape, dtype=np.float32)  # Accumulation
        A_soil = np.zeros(timestep_shape, dtype=np.float32)
        E_biomass = np.zeros(timestep_shape, dtype=np.float32)  # Emissions
        E_soil = np.zeros(timestep_shape, dtype=np.float32)
        # Net Sequestration
        N_biomass = np.zeros(timestep_shape, dtype=np.float32)
        N_soil = np.zeros(timestep_shape, dtype=np.float32)
        V = np.zeros(timestep_shape, dtype=np.float32)  # Valuation

        snapshot_shape = (d['transitions']+1, x_size, y_size)
        L = np.zeros(snapshot_shape, dtype=np.float32)  # Litter

        transition_shape = (d['transitions'], x_size, y_size)
        # Yearly Accumulation
        Y_biomass = np.zeros(transition_shape, dtype=np.float32)
        Y_soil = np.zeros(transition_shape, dtype=np.float32)
        # Disturbance Percentage
        D_biomass = np.zeros(transition_shape, dtype=np.float32)
        D_soil = np.zeros(transition_shape, dtype=np.float32)
        H_biomass = np.zeros(transition_shape, dtype=np.float32)  # Half-life
        H_soil = np.zeros(transition_shape, dtype=np.float32)
        # Total Disturbed Carbon
        R_biomass = np.zeros(transition_shape, dtype=np.float32)
        R_soil = np.zeros(transition_shape, dtype=np.float32)

        # Set Accumulation and Disturbance Values
        C_r = [read_from_raster(i, offset_dict) for i in d['C_r_rasters']]
        C_list = [C_prior] + C_r
        for i in xrange(0, d['transitions']):
            D_biomass[i] = reclass_transition(
                C_list[i],
                C_list[i+1],
                d['lulc_trans_to_Db'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            D_soil[i] = reclass_transition(
                C_list[i],
                C_list[i+1],
                d['lulc_trans_to_Ds'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            H_biomass[i] = reclass(
                C_list[i],
                d['lulc_to_Hb'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            H_soil[i] = reclass(
                C_list[i], d['lulc_to_Hs'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            Y_biomass[i] = reclass(
                C_list[i+1], d['lulc_to_Yb'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)
            Y_soil[i] = reclass(
                C_list[i+1],
                d['lulc_to_Ys'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)

        S_biomass[0] = reclass(
            C_prior,
            d['lulc_to_Sb'],
            out_dtype=np.float32,
            nodata_mask=C_nodata)
        S_soil[0] = reclass(
            C_prior,
            d['lulc_to_Ss'],
            out_dtype=np.float32,
            nodata_mask=C_nodata)

        for i in xrange(0, len(C_list)):
            L[i] = reclass(
                C_list[i],
                d['lulc_to_L'],
                out_dtype=np.float32,
                nodata_mask=C_nodata)

        T[0] = S_biomass[0] + S_soil[0]

        R_biomass[0] = D_biomass[0] * S_biomass[0]
        R_soil[0] = D_soil[0] * S_soil[0]

        # Transient Analysis
        for i in xrange(0, timesteps):
            transition_idx = timestep_to_transition_idx(
                d['snapshot_years'], d['transitions'], i)

            if is_transition_year(d['snapshot_years'], d['transitions'], i):
                # Set disturbed stock values
                R_biomass[transition_idx] = \
                    D_biomass[transition_idx] * S_biomass[i]
                R_soil[transition_idx] = D_soil[transition_idx] * S_soil[i]

            # Accumulation
            A_biomass[i] = Y_biomass[transition_idx]
            A_soil[i] = Y_soil[transition_idx]

            # Emissions
            for transition_idx in xrange(0, timestep_to_transition_idx(
                    d['snapshot_years'], d['transitions'], i)+1):
                j = d['transition_years'][transition_idx] - \
                        d['transition_years'][0]
                E_biomass[i] += R_biomass[transition_idx] * \
                    (0.5**(i-j) - 0.5**(i-j+1))
                E_soil[i] += R_soil[transition_idx] * \
                    (0.5**(i-j) - 0.5**(i-j+1))

            # Net Sequestration
            N_biomass[i] = A_biomass[i] - E_biomass[i]
            N_soil[i] = A_soil[i] - E_soil[i]

            # Next Stock
            S_biomass[i+1] = S_biomass[i] + N_biomass[i]
            S_soil[i+1] = S_soil[i] + N_soil[i]
            T[i+1] = S_biomass[i+1] + S_soil[i+1]

            # Net Present Value
            if d['do_economic_analysis']:
                V[i] = (N_biomass[i] + N_soil[0]) * d['price_t'][i]

        # Write outputs: T_s, A_r, E_r, N_r, NPV
        s_years = d['snapshot_years']
        num_snapshots = len(s_years)

        A = A_biomass + A_soil
        E = E_biomass + E_soil
        N = N_biomass + N_soil

        A_r = [sum(A[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]
        E_r = [sum(E[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]
        N_r = [sum(N[s_to_timestep(s_years, i):s_to_timestep(s_years, i+1)])
               for i in xrange(0, num_snapshots-1)]

        T_s = [T[s_to_timestep(s_years, i)] for i in xrange(0, num_snapshots)]

        # Add litter to total carbon stock
        if len(T_s) == len(L):
            T_s = map(np.add, T_s, L)
        else:
            T_s = map(np.add, T_s, L[:-1])

        N_total = sum(N)

        raster_tuples = [
            ('T_s_rasters', T_s),
            ('A_r_rasters', A_r),
            ('E_r_rasters', E_r),
            ('N_r_rasters', N_r)]

        for key, array in raster_tuples:
            for i in xrange(0, len(d['File_Registry'][key])):
                write_to_raster(
                    d['File_Registry'][key][i],
                    array[i],
                    offset_dict['xoff'],
                    offset_dict['yoff'])

        write_to_raster(
            d['File_Registry']['N_total_raster'],
            N_total,
            offset_dict['xoff'],
            offset_dict['yoff'])

        if d['do_economic_analysis']:
            NPV = np.sum(V, axis=0)
            write_to_raster(
                d['File_Registry']['NPV_raster'],
                NPV,
                offset_dict['xoff'],
                offset_dict['yoff'])

    LOGGER.info("...Coastal Blue Carbon model run complete.")


def timestep_to_transition_idx(snapshot_years, transitions, timestep):
    """Convert timestep to transition index.

    Args:
        snapshot_years (list): a list of years corresponding to the provided
            rasters
        transitions (int): the number of transitions in the scenario
        timestep (int): the current timestep

    Returns:
        transition_idx (int): the current transition
    """
    for i in xrange(0, transitions):
        if timestep < (snapshot_years[i+1] - snapshot_years[0]):
            return i


def s_to_timestep(snapshot_years, snapshot_idx):
    """Convert snapshot index position to timestep.

    Args:
        snapshot_years (list): list of snapshot years.
        snapshot_idx (int): index of snapshot

    Returns:
        snapshot_timestep (int): timestep of the snapshot
    """
    return snapshot_years[snapshot_idx] - snapshot_years[0]


def is_transition_year(snapshot_years, transitions, timestep):
    """Check whether given timestep is a transition year.

    Args:
        snapshot_years (list): list of snapshot years.
        transitions (int): number of transitions.
        timestep (int): current timestep.

    Returns:
        is_transition_year (bool): whether the year corresponding to the
            timestep is a transition year.
    """
    if (timestep_to_transition_idx(snapshot_years, transitions, timestep) !=
        timestep_to_transition_idx(snapshot_years, transitions, timestep-1) and
            timestep_to_transition_idx(snapshot_years, transitions, timestep)):
        return True
    return False


def get_num_blocks(raster_uri):
    """Get the number of blocks in a raster file.

    Args:
        raster_uri (str): filepath to raster

    Returns:
        num_blocks (int): number of blocks in raster
    """
    ds = gdal.Open(raster_uri)
    n_rows = ds.RasterYSize
    n_cols = ds.RasterXSize

    band = ds.GetRasterBand(1)
    cols_per_block, rows_per_block = band.GetBlockSize()

    n_col_blocks = int(math.ceil(n_cols / float(cols_per_block)))
    n_row_blocks = int(math.ceil(n_rows / float(rows_per_block)))

    ds = None

    return n_col_blocks * n_row_blocks


def reclass(array, d, out_dtype=None, nodata_mask=None):
    """Reclassify values in array.

    If a nodata value is not provided, the function will return an array with
    NaN values in its place to mark cells that could not be reclassed.​

    Args:
        array (np.array): input data
        d (dict): reclassification map
        out_dtype (np.dtype): a numpy datatype for the reclass_array
        nodata_mask (number): for floats, a nodata value that is set to np.nan
            if provided to make reclass_array nodata values consistent

    Returns:
        reclass_array (np.array): reclassified array
    """
    if out_dtype:
        array = array.astype(out_dtype)
    u = np.unique(array)
    has_map = np.in1d(u, d.keys())
    if np.issubdtype(out_dtype, int):
        ndata = np.iinfo(out_dtype).min
    else:
        ndata = np.finfo(out_dtype).min

    reclass_array = array.copy()
    for i in u[~has_map]:
        reclass_array = np.where(reclass_array == i, ndata, reclass_array)

    a_ravel = reclass_array.ravel()
    d[ndata] = ndata
    d[np.nan] = np.nan
    k = sorted(d.keys())
    v = np.array([d[key] for key in k])
    index = np.digitize(a_ravel, k, right=True)
    reclass_array = v[index].reshape(array.shape)

    if nodata_mask and np.issubdtype(reclass_array.dtype, float):
        reclass_array[array == nodata_mask] = np.nan
        reclass_array[array == ndata] = np.nan

    return reclass_array


def reclass_transition(a_prev, a_next, trans_dict, out_dtype=None, nodata_mask=None):
    """Reclass arrays based on element-wise combinations between two arrays.

    Args:
        a_prev (np.array): previous lulc array
        a_next (np.array): next lulc array
        trans_dict (dict): reclassification map

    Returns:
        reclass_array (np.array): reclassified array
    """
    a = a_prev.flatten()
    b = a_next.flatten()
    c = np.ma.masked_array(np.zeros(a.shape))
    if out_dtype:
        c = c.astype(out_dtype)

    z = enumerate(itertools.izip(a, b))
    for index, transition_tuple in z:
        if transition_tuple in trans_dict:
            c[index] = trans_dict[transition_tuple]
        else:
            c[index] = np.ma.masked

    if nodata_mask and np.issubdtype(c.dtype, float):
        c[a == nodata_mask] = np.nan

    return c.reshape(a_prev.shape)


def write_to_raster(output_raster, array, xoff, yoff):
    """Write numpy array to raster block.

    Args:
        output_raster (str): filepath to output raster
        array (np.array): block to save to raster
        xoff (int): offset index for x-dimension
        yoff (int): offset index for y-dimension
    """
    ds = gdal.Open(output_raster, gdal.GA_Update)
    band = ds.GetRasterBand(1)
    if np.issubdtype(array.dtype, float):
        array[array == np.nan] = NODATA_FLOAT
    band.WriteArray(array, xoff, yoff)
    ds = None


def read_from_raster(input_raster, offset_block):
    """Read numpy array from raster block.

    Args:
        input_raster (str): filepath to input raster
        offset_block (dict): dictionary of offset information

    Returns:
        array (np.array): a blocked array of the input raster
    """
    ds = gdal.Open(input_raster)
    band = ds.GetRasterBand(1)
    array = band.ReadAsArray(**offset_block)
    ds = None
    return array


def get_inputs(args):
    """Get Inputs.

    Parameters:
        workspace_dir (str): workspace directory
        results_suffix (str): optional suffix appended to results
        lulc_lookup_uri (str): lulc lookup table filepath
        lulc_transition_matrix_uri (str): lulc transition table filepath
        carbon_pool_initial_uri (str): initial conditions table filepath
        carbon_pool_transient_uri (str): transient conditions table filepath
        lulc_baseline_map_uri (str): baseline map filepath
        lulc_transition_maps_list (list): ordered list of transition map
            filepaths
        lulc_transition_years_list (list): ordered list of transition years
        analysis_year (int): optional final year to extend the analysis beyond
            the last transition year
        do_economic_analysis (bool): whether to run economic component of
            the analysis
        do_price_table (bool): whether to use the price table for the economic
            component of the analysis
        price (float): the price of net sequestered carbon
        interest_rate (float): the interest rate on the price of carbon
        price_table_uri (str): price table filepath
        discount_rate (float): the discount rate on future valuations of carbon

    Returns:
        d (dict): data dictionary.

    Example Returns:
        d = {
            'workspace_dir': <string>,
            'transition_years': <list>,
            'analysis_year': <int>,
            'snapshot_years': <list>
            'timesteps': <int>,
            'transitions': <int>,
            'do_economic_analysis': <bool>,
            'price_t': <dict>,
            'lulc_to_Sb': <dict>,
            'lulc_to_Ss': <dict>,
            'lulc_to_L': <dict>,
            'lulc_to_Yb': <dict>,
            'lulc_to_Ys': <dict>,
            'lulc_to_Hb': <dict>,
            'lulc_to_Hs': <dict>,
            'lulc_trans_to_Db': <dict>,
            'lulc_trans_to_Ds': <dict>,
            'C_s': <list>,
            'Y_pr': <dict>,
            'D_pr': <dict>,
            'H_pr': <dict>,
            'L_s': <list>,
            'A_pr': <dict>,
            'E_pr': <dict>,
            'S_pb': <dict>,
            'T_b': <list>,
            'N_pr': <dict>,
            'N_r': <list>,
            'N': <string>,
            'V': <string>
        }

    """
    d = {
        'workspace_dir': None,
        'border_year_list': None,
        'do_economic_analysis': False,
        'lulc_to_Sb': {'lulc': 'biomass'},
        'lulc_to_Ss': {'lulc': 'soil'},
        'lulc_to_L': {'lulc': 'litter'},
        'lulc_to_Yb': {'lulc': 'accum-bio'},
        'lulc_to_Ys': {'lulc': 'accum-soil'},
        'lulc_to_Hb': {'lulc': 'hl-bio'},
        'lulc_to_Hs': {'lulc': 'hl-soil'},
        'lulc_trans_to_Db': {('lulc1', 'lulc2'): 'dist-val'},
        'lulc_trans_to_Ds': {('lulc1', 'lulc2'): 'dist-val'},
        'C_s': [],
        'C_prior': None,
        'C_r_rasters': [],
        'transition_years': [],
        'analysis_year': None,
        'snapshot_years': [],
        'timesteps': None,
        'transitions': None,
        'interest_rate': None,
        'price_t': None,
    }

    # Directories
    results_suffix = invest_utils.make_suffix_string(
        args, 'results_suffix')
    d['workspace_dir'] = args['workspace_dir']
    outputs_dir = os.path.join(args['workspace_dir'], 'outputs_core')
    geoprocess.create_directories([args['workspace_dir'], outputs_dir])

    # Rasters
    d['transition_years'] = [int(i) for i in
                             args['lulc_transition_years_list']]
    for i in range(0, len(d['transition_years'])-1):
        if d['transition_years'][i] >= d['transition_years'][i+1]:
            raise ValueError(
                'LULC snapshot years must be provided in chronological order.'
                ' and in the same order as the LULC snapshot rasters.')
    d['transitions'] = len(d['transition_years'])

    d['snapshot_years'] = d['transition_years'][:]
    if 'analysis_year' in args and args['analysis_year'] not in ['', None]:
        if int(args['analysis_year']) <= d['snapshot_years'][-1]:
            raise ValueError(
                'Analysis year must be greater than last transition year.')
        d['snapshot_years'].append(int(args['analysis_year']))
    d['timesteps'] = d['snapshot_years'][-1] - d['snapshot_years'][0]

    d['C_prior_raster'] = args['lulc_baseline_map_uri']
    d['C_r_rasters'] = args['lulc_transition_maps_list']

    # Reclass Dictionaries
    lulc_lookup_dict = geoprocess.get_lookup_from_csv(
        args['lulc_lookup_uri'], 'lulc-class')
    lulc_to_code_dict = \
        dict((k.lower(), v['code']) for k, v in lulc_lookup_dict.items())
    initial_dict = geoprocess.get_lookup_from_csv(
            args['carbon_pool_initial_uri'], 'lulc-class')

    code_dict = dict((lulc_to_code_dict[k.lower()], s) for (k, s)
        in initial_dict.iteritems())
    for args_key, col_name in [('lulc_to_Sb', 'biomass'),
        ('lulc_to_Ss', 'soil'), ('lulc_to_L', 'litter')]:
            d[args_key] = dict(
                (code, row[col_name]) for code, row in code_dict.iteritems())

    # Transition Dictionaries
    biomass_transient_dict, soil_transient_dict = \
        _create_transient_dict(args['carbon_pool_transient_uri'])

    d['lulc_to_Yb'] = dict((key, sub['yearly-accumulation'])
                           for key, sub in biomass_transient_dict.items())
    d['lulc_to_Ys'] = dict((key, sub['yearly-accumulation'])
                           for key, sub in soil_transient_dict.items())
    d['lulc_to_Hb'] = dict((key, sub['half-life'])
                           for key, sub in biomass_transient_dict.items())
    d['lulc_to_Hs'] = dict((key, sub['half-life'])
                           for key, sub in soil_transient_dict.items())

    # Parse LULC Transition CSV (Carbon Direction and Relative Magnitude)
    d['lulc_trans_to_Db'], d['lulc_trans_to_Ds'] = _get_lulc_trans_to_D_dicts(
        args['lulc_transition_matrix_uri'],
        args['lulc_lookup_uri'],
        biomass_transient_dict,
        soil_transient_dict)

    # Economic Analysis
    d['do_economic_analysis'] = False
    if args['do_economic_analysis']:
        d['do_economic_analysis'] = True
        # convert percentage to decimal
        discount_rate = float(args['discount_rate']) * 0.01
        if args['do_price_table']:
            d['price_t'] = _get_price_table(
                args['price_table_uri'],
                d['snapshot_years'][0],
                d['snapshot_years'][-1])
        else:
            interest_rate = float(args['interest_rate']) * 0.01
            price = args['price']
            d['price_t'] = (1 + interest_rate) ** np.arange(
                0, d['timesteps']+1) * price

        d['price_t'] /= (1 + discount_rate) ** np.arange(0, d['timesteps']+1)

    # Create Output Rasters
    if args['results_suffix'] not in ['', None] and not args['results_suffix'].startswith('_'):
        args['results_suffix'] = '_' + args['results_suffix']
    d['File_Registry'] = _build_file_registry(
        d['C_prior_raster'],
        d['snapshot_years'],
        args['results_suffix'],
        d['do_economic_analysis'],
        outputs_dir)

    return d


def _build_file_registry(C_prior_raster, snapshot_years, results_suffix,
                         do_economic_analysis, outputs_dir):
    """Build an output file registry.

    Args:
        C_prior_raster (str): template raster
        snapshot_years (list): years of provided snapshots to help with
            filenames
        results_suffix (str): the results file suffix
        do_economic_analysis (bool): whether or not to create a NPV raster
        outputs_dir (str): path to output directory

    Returns:
        File_Registry (dict): map to collections of output files.
    """
    template_raster = C_prior_raster

    _INTERMEDIATE = {
        'carbon_stock': 'carbon_stock_at_%s.tif',
        'carbon_accumulation': 'carbon_accumulation_between_%s_and_%s.tif',
        'cabon_emissions': 'carbon_emissions_between_%s_and_%s.tif',
        'carbon_net_sequestration': 'net_carbon_sequestration_between_%s_and_%s.tif',
    }

    T_s_rasters = []
    A_r_rasters = []
    E_r_rasters = []
    N_r_rasters = []

    for snapshot_idx in xrange(len(snapshot_years)-1):
        snapshot_year = snapshot_years[snapshot_idx]
        next_snapshot_year = snapshot_years[snapshot_idx + 1]
        T_s_rasters.append(_INTERMEDIATE['carbon_stock'] % (snapshot_year))
        A_r_rasters.append(_INTERMEDIATE['carbon_accumulation'] % (snapshot_year, next_snapshot_year))
        E_r_rasters.append(_INTERMEDIATE['cabon_emissions'] % (snapshot_year, next_snapshot_year))
        N_r_rasters.append(_INTERMEDIATE['carbon_net_sequestration'] % (snapshot_year, next_snapshot_year))
    T_s_rasters.append(_INTERMEDIATE['carbon_stock'] % (snapshot_years[-1]))

    # Total Net Sequestration
    N_total_raster = 'total_net_carbon_sequestration.tif'

    # Net Sequestration from Base Year to Analysis Year
    NPV_raster = None
    if do_economic_analysis:
        NPV_raster = 'net_present_value.tif'

    file_registry = invest_utils.build_file_registry([({
            'T_s_rasters': T_s_rasters,
            'A_r_rasters': A_r_rasters,
            'E_r_rasters': E_r_rasters,
            'N_r_rasters': N_r_rasters,
            'N_total_raster': N_total_raster,
            'NPV_raster': NPV_raster
        }, outputs_dir)], results_suffix)

    raster_lists = ['T_s_rasters', 'A_r_rasters', 'E_r_rasters', 'N_r_rasters']
    for raster_filepath in itertools.chain(*[file_registry[key] for key in raster_lists]):
        geoprocess.new_raster_from_base_uri(
            template_raster, raster_filepath, 'GTiff', NODATA_FLOAT, gdal.GDT_Float32)
    for raster_key in ['N_total_raster', 'NPV_raster']:
        if file_registry[raster_key] is not None:
            geoprocess.new_raster_from_base_uri(
                template_raster, file_registry[raster_key], 'GTiff', NODATA_FLOAT, gdal.GDT_Float32)

    return file_registry


def _get_lulc_trans_to_D_dicts(lulc_transition_uri, lulc_lookup_uri,
                               biomass_transient_dict, soil_transient_dict):
    """Get the lulc_trans_to_D dictionaries.

    Args:
        lulc_transition_uri (str): transition matrix table
        lulc_lookup_uri (str): lulc lookup table
        biomass_transient_dict (dict): transient biomass values
        soil_transient_dict (dict): transient soil values

    Returns:
        lulc_trans_to_Db (dict): biomass transition values
        lulc_trans_to_Ds (dict): soil transition values

    Example Returns:
        lulc_trans_to_Db = {
            (lulc-1, lulc-2): dist-val,
            (lulc-1, lulc-3): dist-val,
            ...
        }
    """
    lulc_transition_dict = geoprocess.get_lookup_from_csv(
        lulc_transition_uri, 'lulc-class')
    lulc_lookup_dict = geoprocess.get_lookup_from_csv(
        lulc_lookup_uri, 'lulc-class')
    lulc_to_code_dict = \
        dict((k, v['code']) for k, v in lulc_lookup_dict.items())

    lulc_trans_to_Db = {}
    lulc_trans_to_Ds = {}
    for k, sub in lulc_transition_dict.items():
        # the line below serves to break before legend in CSV file
        if k is not '':
            continue
        for k2, v in sub.items():
            if k2 is not '' and v.endswith('disturb'):
                lulc_trans_to_Db[(
                    lulc_to_code_dict[k], lulc_to_code_dict[k2])] = \
                    biomass_transient_dict[k][v]
                lulc_trans_to_Ds[(
                    lulc_to_code_dict[k], lulc_to_code_dict[k2])] = \
                    soil_transient_dict[k][v]

    return lulc_trans_to_Db, lulc_trans_to_Ds


def _create_transient_dict(carbon_pool_transient_uri):
    """Create dictionary of transient variables for carbon pools.

    Parameters:
        carbon_pool_transient_uri (string): path to carbon pool transient
            variables csv file.

    Returns:
        biomass_transient_dict (dict): transient biomass values
        soil_transient_dict (dict): transient soil values
    """
    transient_dict = geoprocess.get_lookup_from_table(
        carbon_pool_transient_uri, 'code')
    biomass_transient_dict = {}
    soil_transient_dict = {}
    for code, subdict in transient_dict.items():
        biomass_transient_dict[code] = {}
        soil_transient_dict[code] = {}
        for key, val in subdict.items():
            if key.lower().startswith('biomass'):
                biomass_transient_dict[code][key.lstrip('biomass')[1:]] = val
            if key.lower().startswith('soil'):
                soil_transient_dict[code][key.lstrip('soil')[1:]] = val
        biomass_transient_dict[code]['lulc-class'] = subdict['lulc-class']
        soil_transient_dict[code]['lulc-class'] = subdict['lulc-class']

    return biomass_transient_dict, soil_transient_dict


def _get_price_table(price_table_uri, start_year, end_year):
    """Get price table.

    Parameters:
        price_table_uri (str): filepath to price table csv file
        start_year (int): start year of analysis
        end_year (int): end year of analysis

    Returns:
        price_t (np.array): price for each year.
    """
    price_dict = geoprocess.get_lookup_from_table(price_table_uri, 'year')

    try:
        return np.array([price_dict[year-start_year]['price']
                        for year in xrange(start_year, end_year+1)])
    except KeyError as missing_year:
        raise KeyError('Carbon price table does not contain a price value for '
                       '%s' % missing_year)
