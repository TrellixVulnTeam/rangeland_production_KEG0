# coding=UTF-8
# -----------------------------------------------
# Generated by InVEST 3.6.0.post206+hb14f3a2cdb18 on 03/27/19 14:28:23
# Model: Nutrient Delivery Ratio Model (NDR)

import logging

import natcap.invest.ndr.ndr

logging.basicConfig(level=logging.DEBUG)

args = {
    u'biophysical_table_path': u'C:\\Users\\rpsharp\\Documents\\bitbucket_repos\\invest\\data\\invest-data\\Base_Data\\Freshwater\\biophysical_table.csv',
    u'calc_n': True,
    u'calc_p': True,
    u'dem_path': u'C:\\Users\\rpsharp\\Documents\\bitbucket_repos\\invest\\data\\invest-data\\Base_Data\\Freshwater\\dem',
    u'k_param': u'2',
    u'lulc_path': u'C:\\Users\\rpsharp\\Documents\\bitbucket_repos\\invest\\data\\invest-data\\Base_Data\\Freshwater\\landuse_90',
    u'results_suffix': u'',
    u'runoff_proxy_path': u'C:\\Users\\rpsharp\\Documents\\bitbucket_repos\\invest\\data\\invest-data\\Base_Data\\Freshwater\\precip',
    u'subsurface_critical_length_n': u'150',
    u'subsurface_critical_length_p': u'150',
    u'subsurface_eff_n': u'0.8',
    u'subsurface_eff_p': u'0.8',
    u'threshold_flow_accumulation': u'1000',
    u'watersheds_path': u'C:\\Users\\rpsharp\\Documents\\bitbucket_repos\\invest\\data\\invest-data\\Base_Data\\Freshwater\\watersheds.shp',
    u'workspace_dir': 'test_ndr_workspace',
}

if __name__ == '__main__':
    natcap.invest.ndr.ndr.execute(args)
