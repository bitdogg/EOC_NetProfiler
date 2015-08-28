# Copyright (c) 2015 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the MIT License
# accompanying the software ("License").  This software is distributed "AS IS"
# as set forth in the License.


import time
import logging
import threading
import datetime
import pandas
import types
import json
from collections import namedtuple

from django import forms

from steelscript.netprofiler.core.services import \
    Service, ServiceLocationReport
from steelscript.netprofiler.core.report import \
    Report, SingleQueryReport, TrafficTimeSeriesReport
from steelscript.netprofiler.core.filters import TimeFilter, TrafficFilter
from steelscript.common.timeutils import (parse_timedelta,
                                          timedelta_total_seconds)
from steelscript.appfwk.apps.datasource.models import \
    DatasourceTable, Column, TableQueryBase, Table
from steelscript.appfwk.apps.datasource.models import TableField
from steelscript.appfwk.apps.devices.forms import fields_add_device_selection
from steelscript.appfwk.apps.devices.devicemanager import DeviceManager
from steelscript.appfwk.apps.datasource.forms import \
    fields_add_time_selection, fields_add_resolution
from steelscript.appfwk.libs.fields import Function
from steelscript.netprofiler.core.hostgroup import HostGroupType
from steelscript.appfwk.apps.jobs import QueryComplete

logger = logging.getLogger(__name__)
lock = threading.Lock()


def _post_process_combine_filterexprs(form, id, criteria, params):
    exprs = []
    if ('netprofiler_filterexpr' in criteria and
            criteria.netprofiler_filterexpr != ''):
        exprs.append(criteria.netprofiler_filterexpr)

    field = form.get_tablefield(id)
    for parent in field.parent_keywords:
        expr = criteria[parent]
        if expr is not None and expr != '':
            exprs.append(expr)

    if len(exprs) == 0:
        val = ""
    elif len(exprs) == 1:
        val = exprs[0]
    else:
        val = "(" + ") and (".join(exprs) + ")"

    criteria['netprofiler_filterexpr'] = val


def netprofiler_hostgroup_types(form, id, field_kwargs, params):
    """ Query netprofiler for all hostgroup types. """
    netprofiler_device = form.get_field_value('netprofiler_device', id)

    if netprofiler_device == '':
        choices = [('', '<No netprofiler device>')]
    else:
        netprofiler = DeviceManager.get_device(netprofiler_device)

        choices = []

        for hgt in netprofiler.api.host_group_types.get_all():
            choices.append((hgt['name'], hgt['name']))

    field_kwargs['label'] = 'HostGroupType'
    field_kwargs['choices'] = choices


def netprofiler_hostgroups(form, id, field_kwargs, params):
    """ Query netprofiler for groups within a given hostgroup. """
    netprofiler_device = form.get_field_value('netprofiler_device', id)

    if netprofiler_device == '':
        choices = [('', '<No netprofiler device>')]
    else:
        netprofiler = DeviceManager.get_device(netprofiler_device)

        if params is not None and 'hostgroup_type' in params:
            hgt = HostGroupType.find_by_name(netprofiler,
                                             params['hostgroup_type'])
        else:
            hostgroup_type = form.get_field_value('hostgroup_type', id)

            hgt = HostGroupType.find_by_name(netprofiler,
                                             hostgroup_type)

        choices = [(group, group) for group in hgt.groups.keys()]

    field_kwargs['label'] = 'HostGroup'
    field_kwargs['choices'] = choices


def add_netprofiler_hostgroup_field(report, section, hg_type=None):
    """ Attach fields for dynamic HostGroup dropdowns to add as filter
    expressions to the report.

    This can be added for each section in a report where the added filter
    expression is desired.

    The optional ``hg_type`` argument can be either a single string or a list
    of strings for each HostGroupType.  If a single string, the
    'HostGroupType' field will be hidden and automatically filter HostGroups
    to the given HostGroupType.  If a list, the elements of the HostGroupType
    list will be fixed to those in the list; this can be helpful if certain
    HostGroupTypes may be sensitive or not applicable to the report.
    """
    # add default filter expr to extend against
    filterexpr = TableField.create(keyword='netprofiler_filterexpr')
    section.fields.add(filterexpr)

    # defaults if we are using hostgroup type field
    hg_template = '{hostgroup_type}'
    hg_parent = ['hostgroup_type']
    hg_params = None

    if hg_type is None:
        # add hostgroup types field that queries netprofiler
        field = TableField.create(
            keyword='hostgroup_type',
            label='HostGroup Type',
            obj=report,
            field_cls=forms.ChoiceField,
            parent_keywords=['netprofiler_device'],
            dynamic=True,
            pre_process_func=Function(netprofiler_hostgroup_types)
        )
        section.fields.add(field)

    elif type(hg_type) in (list, tuple):
        # add hostgroup types field that uses given list
        field = TableField.create(
            keyword='hostgroup_type',
            label='HostGroup Type',
            obj=report,
            field_cls=forms.ChoiceField,
            field_kwargs={'choices': zip(hg_type, hg_type)},
            parent_keywords=['netprofiler_device'],
        )
        section.fields.add(field)

    else:
        # no field, hardcode the given value
        hg_template = hg_type
        hg_parent = None
        hg_params = {'hostgroup_type': hg_type}

    # add hostgroup field
    field = TableField.create(
        keyword='hostgroup',
        label='HostGroup',
        obj=report,
        field_cls=forms.ChoiceField,
        parent_keywords=hg_parent,
        dynamic=True,
        pre_process_func=Function(netprofiler_hostgroups, params=hg_params)
    )
    section.fields.add(field)

    NetProfilerTable.extend_filterexpr(
        section, keyword='hg_filterexpr',
        template='hostgroup %s:{hostgroup}' % hg_template
    )


class NetProfilerTable(DatasourceTable):

    class Meta:
        proxy = True
    _query_class = 'NetProfilerQuery'

    TABLE_OPTIONS = {'groupby': None,
                     'realm': None,
                     'interface': None}

    # default field parameters
    FIELD_OPTIONS = {'duration': 60,
                     'durations': ('15 min', '1 hour',
                                   '2 hours', '4 hours', '12 hours',
                                   '1 day', '1 week', '4 weeks'),
                     'resolution': 'auto',
                     'resolutions': (('auto', 'Automatic'),
                                     '1min', '15min', 'hour', '6hour'),
                     }

    def post_process_table(self, field_options):
        resolution = field_options['resolution']
        if resolution != 'auto':
            if isinstance(resolution, int):
                res = resolution
            else:
                res = int(timedelta_total_seconds(parse_timedelta(resolution)))
            resolution = Report.RESOLUTION_MAP[res]
            field_options['resolution'] = resolution

        fields_add_device_selection(self, keyword='netprofiler_device',
                                    label='NetProfiler', module='netprofiler',
                                    enabled=True)

        duration = field_options['duration']
        if isinstance(duration, int):
            duration = "%d min" % duration

        fields_add_time_selection(self,
                                  initial_duration=duration,
                                  durations=field_options['durations'])

        fields_add_resolution(self,
                              initial=field_options['resolution'],
                              resolutions=field_options['resolutions'],
                              special_values=['auto'])
        self.fields_add_filterexpr()

    def fields_add_filterexpr(self, keyword='netprofiler_filterexpr',
                              initial=None):
        field = TableField(keyword=keyword,
                           label='NetProfiler Filter Expression',
                           help_text=('Traffic expression using NetProfiler '
                                      'Advanced Traffic Expression syntax'),
                           initial=initial,
                           required=False)
        field.save()
        self.fields.add(field)

    def fields_add_filterexprs_field(self, keyword):

        field = self.fields.get(keyword='netprofiler_filterexpr')
        field.post_process_func = Function(
            function=_post_process_combine_filterexprs
        )

        parent_keywords = set(field.parent_keywords or [])
        parent_keywords.add(keyword)
        field.parent_keywords = list(parent_keywords)
        field.save()

        return field

    @classmethod
    def extend_filterexpr(cls, obj, keyword, template):

        field = obj.fields.get(keyword='netprofiler_filterexpr')
        field.post_process_func = Function(
            function=_post_process_combine_filterexprs
        )

        TableField.create(
            keyword=keyword, obj=obj, hidden=True,
            post_process_template=template)

        parent_keywords = set(field.parent_keywords or [])
        parent_keywords.add(keyword)
        field.parent_keywords = list(parent_keywords)
        field.save()


class NetProfilerTimeSeriesTable(NetProfilerTable):
    class Meta:
        proxy = True

    TABLE_OPTIONS = {'groupby': 'time',
                     'realm': 'traffic_overall_time_series',
                     'interface': None,
                     'limit': None}


class NetProfilerGroupbyTable(NetProfilerTable):
    class Meta:
        proxy = True

    TABLE_OPTIONS = {'groupby': None,
                     'realm': 'traffic_summary',
                     'interface': None,
                     'limit': None}


class NetProfilerQuery(TableQueryBase):

    def _prepare_report_args(self):
        class Args(object):
            pass
        args = Args()

        criteria = self.job.criteria

        if criteria.netprofiler_device == '':
            logger.debug('%s: No netprofiler device selected' % self.table)
            self.job.mark_error("No NetProfiler Device Selected")
            return False

        args.profiler = DeviceManager.get_device(criteria.netprofiler_device)

        args.columns = [col.name for col
                        in self.table.get_columns(synthetic=False)]

        args.sortcol = None
        if self.table.sortcols is not None:
            args.sortcol = self.table.sortcols[0]

        args.timefilter = TimeFilter(start=criteria.starttime,
                                     end=criteria.endtime)

        logger.info("Running NetProfiler table %d report for timeframe %s" %
                    (self.table.id, str(args.timefilter)))

        if ('datafilter' in criteria) and (criteria.datafilter is not None):
            args.datafilter = criteria.datafilter.split(',')
        else:
            args.datafilter = None

        args.trafficexpr = TrafficFilter(
            self.job.combine_filterexprs(exprs=criteria.netprofiler_filterexpr)
        )

        # Incoming criteria.resolution is a timedelta
        logger.debug('NetProfiler report got criteria resolution %s (%s)' %
                     (criteria.resolution, type(criteria.resolution)))
        if criteria.resolution != 'auto':
            rsecs = int(timedelta_total_seconds(criteria.resolution))
            args.resolution = Report.RESOLUTION_MAP[rsecs]
        else:
            args.resolution = 'auto'

        logger.debug('NetProfiler report using resolution %s (%s)' %
                     (args.resolution, type(args.resolution)))

        args.limit = (self.table.options.limit
                      if hasattr(self.table.options, 'limit') else None)

        args.centricity = 'int' if self.table.options.interface else 'hos'

        return args

    def _wait_for_data(self, report, minpct=0, maxpct=100):
        criteria = self.job.criteria
        done = False
        logger.info("Waiting for report to complete")
        while not done:
            time.sleep(0.5)
            with lock:
                s = report.status()

            logger.debug('Status: XXX %s' % str(s))
            pct = int(float(s['percent']) * ((maxpct - minpct)/100.0) + minpct)
            self.job.mark_progress(progress=pct)
            done = (s['status'] == 'completed')

        # Retrieve the data
        with lock:
            data = report.get_data()

            tz = criteria.starttime.tzinfo
            # Update criteria
            query = report.get_query_by_index(0)
            criteria.starttime = (datetime.datetime
                                  .utcfromtimestamp(query.actual_t0)
                                  .replace(tzinfo=tz))
            criteria.endtime = (datetime.datetime
                                .utcfromtimestamp(query.actual_t1)
                                .replace(tzinfo=tz))

        self.job.safe_update(actual_criteria=criteria)
        return data

    def run(self):
        """ Main execution method
        """
        args = self._prepare_report_args()

        with lock:
            report = SingleQueryReport(args.profiler)
            report.run(realm=self.table.options.realm,
                       groupby=args.profiler.groupbys[self.table.options.groupby],
                       centricity=args.centricity,
                       columns=args.columns,
                       timefilter=args.timefilter,
                       trafficexpr=args.trafficexpr,
                       data_filter=args.datafilter,
                       resolution=args.resolution,
                       sort_col=args.sortcol,
                       sync=False,
                       limit=args.limit
                       )

        data = self._wait_for_data(report)

        if self.table.rows > 0:
            data = data[:self.table.rows]

        logger.info("Report %s returned %s rows" % (self.job, len(data)))
        return QueryComplete(data)

#
# Traffic Time Series
#
# This is a timeseries report with criteria per columns, as opposed to just a time series
#

class NetProfilerTrafficTimeSeriesTable(NetProfilerTable):

    class Meta:
        proxy = True

    TABLE_OPTIONS = {'base': None,
                     'groupby': None,
                     'col_criteria': None,
                     'interface': None,
                     'top_n': None,
                     'include_other': False}

    _query_class = 'NetProfilerTrafficTimeSeriesQuery'

    @classmethod
    def process_options(cls, table_options):
        # handle direct id's, table references, or table classes
        # from tables option and transform to simple table id value
        table_options['base'] = Table.to_ref(table_options['base'])
        return table_options

    def post_process_table(self, field_options):
        super(NetProfilerTrafficTimeSeriesTable, self).post_process_table(
            field_options)

        if self.options.top_n is None:
            # If not top-n, the criteria field 'query_columns' must
            # either be a string or an array of column definitions
            # (a string is simply parsed as json to the latter).
            #
            # This criteria field must resolve to an array of
            # field definitions, one per column to be queried
            #
            # An array of column defintions looks like the following:
            #   [ {'name': <name>, 'label': <name>, 'json': <json>},
            #     {'name': <name>, 'label': <name>, 'json': <json>},
            #     ... ]
            #
            # Each element corresponds to a column requested.  <name> is
            # used as the Column.name, <label> is for the Column.label
            # and json is what is passed on to NetProfiler in the POST
            # to create the report
            #
            TableField.create(keyword='query_columns',
                              label='Query columns',
                              obj=self)


TSQ_Tuple = namedtuple('TSQ_Tuple', ['groupby', 'columns', 'parser'])

class NetProfilerTrafficTimeSeriesQuery(NetProfilerQuery):

    # Dictionary of config for running time-series/top-n queries for a
    # requested groupby.  The components are:
    #
    #    groupby:  the groupby to use for the time-series query, which is usually
    #              just the plural form of the standard NetProfiler groupby
    #
    #    columns:  the key column(s) to ask for as part of the query
    #
    #    parser:   the name of the row parsing function that takes a row
    #              and converts the row/keys into the necessary form
    #              as required by the time-series groupby report (in run())
    #
    CONFIG = {
        'port': TSQ_Tuple('ports', ['protoport_parts'], 'parse_port'),
        'application': TSQ_Tuple('applications', ['app_name', 'app_raw'], 'parse_app')
        }

    @classmethod
    def parse_app(cls, row):
        app_name = row[0]
        app_raw = row[1]

        return {'name': app_name,
                'label': app_name,
                'json': {'code': app_raw}}

    @classmethod
    def parse_port(cls, row):
        proto, port = row[0].split('|')

        return {'name': '%s%s' % (proto, port),
                'label': '%s/%s' % (proto, port),
                'json': {'name': '%s/%s' % (proto, port)}}

    # Run a SingleQueryReport based on the requested groupby and
    # return a list of column definitions that will be passed
    # on to the TrafficTimeSeriesReport query_columns argument
    def run_top_n(self, config, args, base_col, minpct, maxpct):
        columns = config.columns + [base_col.name]
        with lock:
            report = SingleQueryReport(args.profiler)
            report.run(
                realm='traffic_summary',
                groupby=args.profiler.groupbys[self.table.options.groupby],
                columns=columns,
                timefilter=args.timefilter,
                trafficexpr=args.trafficexpr,
                resolution=args.resolution,
                sort_col=base_col.name,
                sync=False
                )

        rows = self._wait_for_data(report, minpct=minpct, maxpct=maxpct)

        defs = []
        parser = getattr(self, config.parser)

        for row in rows[:int(self.table.options.top_n)]:
            defs.append(parser(row))

        return defs

    # This is the main run method and will run up to 3 reports
    #
    #   1. Top-N report -- if table.options.top_n is specified, this report
    #      drives what columns are requested
    #
    #   2. TrafficTimeSeriesReport - a time-series report with one column
    #      per requested criteria.
    #
    #   3. Other report -- a time-series report showing all traffic, use to
    #      compute "other" if table.options.include_other
    #
    def run(self):
        args = self._prepare_report_args()
        base_table = Table.from_ref(self.table.options.base)
        base_col = base_table.get_columns()[0]

        if self.table.options.groupby not in self.CONFIG:
            raise ValueError('not supported for groupby=%s' %
                             self.table.options.groupby)

        config = self.CONFIG[self.table.options.groupby]

        # num_reports / cur_report are used to compute min/max pct
        num_reports = (1
                       + (1 if self.table.options.top_n else 0)
                       + (1 if self.table.options.include_other else 0))
        cur_report = 0

        if self.table.options.top_n:
            # Run a top-n report to drive the criteria for each column
            query_column_defs = self.run_top_n(
                config, args, base_col,
                minpct=0, maxpct=(100/num_reports))
            cur_report = cur_report+1
        else:
            query_column_defs = self.job.criteria.query_columns
            if isinstance(query_column_defs, types.StringTypes):
                query_column_defs = json.loads(query_column_defs)

        query_columns = [col['json'] for col in query_column_defs]

        with lock:
            report = TrafficTimeSeriesReport(args.profiler)
            columns = [args.columns[0], base_col.name]
            logger.info("Query Columns: %s" % str(query_columns))
            report.run(centricity=args.centricity,
                       columns=columns,
                       timefilter=args.timefilter,
                       trafficexpr=args.trafficexpr,
                       resolution=args.resolution,
                       sync=False,
                       query_columns_groupby=config.groupby,
                       query_columns=query_columns
                       )

        data = self._wait_for_data(report,
                                   minpct=cur_report * (100/num_reports),
                                   maxpct=(cur_report + 1) * (100/num_reports))
        cur_report = cur_report+1

        df = pandas.DataFrame(data,
                              columns=(['time'] + [col['name'] for
                                                   col in query_column_defs]))

        # Create ephemeral columns for all the data based
        # on the related base table
        for col in query_column_defs:
            Column.create(self.job.table, col['name'], col['label'],
                          ephemeral=self.job, datatype=base_col.datatype,
                          formatter=base_col.formatter)

        if self.table.options.include_other:
            # Run a separate timeseries query with no column filters
            # to get "totals" then use that to compute an "other" column

            with lock:
                report = SingleQueryReport(args.profiler)
                report.run(realm='traffic_overall_time_series',
                           groupby=args.profiler.groupbys['time'],
                           columns=columns,
                           timefilter=args.timefilter,
                           trafficexpr=args.trafficexpr,
                           resolution=args.resolution,
                           sync=False
                           )

            totals = self._wait_for_data(report,
                                         minpct=cur_report * (100/num_reports),
                                         maxpct=(cur_report + 1) * (100/num_reports))

            df = df.set_index('time')
            df['subtotal'] = df.sum(axis=1)
            totals_df = (pandas.DataFrame(totals, columns=['time', 'total'])
                         .set_index('time'))

            df = df.merge(totals_df, left_index=True, right_index=True)
            df['other'] = df['total'] = df['subtotal']
            colnames = ['time'] + [col['name'] for col in query_column_defs] + ['other']

            # Drop the extraneous total and subtotal columns
            df = (df.reset_index().ix[:, colnames])

            Column.create(self.job.table, 'other', 'Other',
                          ephemeral=self.job, datatype=base_col.datatype,
                          formatter=base_col.formatter)

        logger.info("Report %s returned %s rows" % (self.job, len(df)))
        return QueryComplete(df)


#
# Service reports
#

class NetProfilerServiceByLocTable(DatasourceTable):

    class Meta:
        proxy = True
    _query_class = 'NetProfilerServiceByLocQuery'

    # rgb - red/yellow/green, if True return string values
    #       instead of numbers
    TABLE_OPTIONS = {'rgb': True}

    FIELD_OPTIONS = {'duration': '15min',
                     'durations': ('15 min', '1 hour',
                                   '2 hours', '4 hours', '12 hours',
                                   '1 day', '1 week', '4 weeks'),
                     }

    def post_process_table(self, field_options):
        fields_add_device_selection(self, keyword='netprofiler_device',
                                    label='NetProfiler', module='netprofiler',
                                    enabled=True)

        duration = field_options['duration']

        fields_add_time_selection(self,
                                  initial_duration=duration,
                                  durations=field_options['durations'])


class NetProfilerServiceByLocQuery(TableQueryBase):

    def run(self):
        """ Main execution method
        """
        criteria = self.job.criteria

        if criteria.netprofiler_device == '':
            logger.debug('%s: No netprofiler device selected' % self.table)
            self.job.mark_error("No NetProfiler Device Selected")
            return False

        profiler = DeviceManager.get_device(criteria.netprofiler_device)
        report = ServiceLocationReport(profiler)

        tf = TimeFilter(start=criteria.starttime,
                        end=criteria.endtime)

        logger.info(
            'Running NetProfilerServiceByLocTable %d report for timeframe %s' %
            (self.table.id, str(tf)))

        with lock:
            report.run(timefilter=tf, sync=False)

        done = False
        logger.info("Waiting for report to complete")
        while not done:
            time.sleep(0.5)
            with lock:
                s = report.status()

            self.job.mark_progress(progress=int(s['percent']))
            done = (s['status'] == 'completed')

        # Retrieve the data
        with lock:
            data = report.get_data()
            query = report.get_query_by_index(0)

            tz = criteria.starttime.tzinfo
            # Update criteria
            criteria.starttime = (datetime.datetime
                                  .utcfromtimestamp(query.actual_t0)
                                  .replace(tzinfo=tz))
            criteria.endtime = (datetime.datetime
                                .utcfromtimestamp(query.actual_t1)
                                .replace(tzinfo=tz))

        self.job.safe_update(actual_criteria=criteria)

        if len(data) == 0:
            return QueryComplete(None)

        # Add ephemeral columns for everything
        Column.create(self.job.table, 'location', 'Location',
                      ephemeral=self.job, datatype='string')
        for k in data[0].keys():
            if k == 'location':
                continue

            Column.create(self.job.table, k, k,
                          ephemeral=self.job, datatype='string',
                          formatter='rvbd.formatHealth')

        df = pandas.DataFrame(data)

        if self.job.table.options.rgb:
            state_map = {Service.SVC_NOT_AVAILABLE: 'gray',
                         Service.SVC_DISABLED: 'gray',
                         Service.SVC_INIT: 'gray',
                         Service.SVC_NORMAL: 'green',
                         Service.SVC_LOW: 'yellow',
                         Service.SVC_MED: 'yellow',
                         Service.SVC_HIGH: 'red',
                         Service.SVC_NODATA: 'gray'}

            df = df.replace(state_map.keys(),
                            state_map.values())

        return QueryComplete(df)
