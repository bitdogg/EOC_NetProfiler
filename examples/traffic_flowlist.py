#!/usr/bin/env python

# Copyright (c) 2019 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the MIT License
# accompanying the software ("License").  This software is distributed "AS IS"
# as set forth in the License.


from steelscript.netprofiler.core.app import NetProfilerApp
from steelscript.netprofiler.core.report import TrafficFlowListReport
from steelscript.netprofiler.core.filters import TimeFilter, TrafficFilter
from steelscript.common.datautils import Formatter

import optparse


class TrafficFlowListApp(NetProfilerApp):

    def add_options(self, parser):
        super(TrafficFlowListApp, self).add_options(parser)
        group = optparse.OptionGroup(parser, "Report Parameters")
        group.add_option('--columns', dest='columns',
                         help='Comma-separated list of column names and/or '
                              'ID numbers, required')
        parser.add_option_group(group)

        group = optparse.OptionGroup(parser, "Filter Options")
        group.add_option('--timefilter', dest='timefilter', default='last 1 hour',
                         help='Time range to analyze (defaults to "last 1 hour") '
                              'other valid formats are: "4/21/13 4:00 to 4/21/13 5:00" '
                              'or "16:00:00 to 21:00:04.546"')

        group.add_option('--trafficexpr', dest='trafficexpr', default=None,
                         help='Traffic Expression to apply to report (default None)')
        parser.add_option_group(group)

        group = optparse.OptionGroup(parser, "Output options")
        group.add_option('--sort', dest='sortby', default=None,
                         help='Column name to sort by (defaults to None)')
        group.add_option('--csv', dest='as_csv', default=False, action='store_true',
                         help='Return values in CSV format instead of tabular')
        parser.add_option_group(group)

    def validate_args(self):
        """ Ensure columns are included
        """
        super(TrafficFlowListApp, self).validate_args()

        if not self.options.columns:
            self.parser.error('Comma-separated list of columns is required.')

    def print_data(self, data, header):
        if self.options.as_csv:
            Formatter.print_csv(data, header)
        else:
            Formatter.print_table(data, header)

    def main(self):
        self.timefilter = TimeFilter.parse_range(self.options.timefilter)
        if self.options.trafficexpr:
            self.trafficexpr = TrafficFilter(self.options.trafficexpr)
        else:
            self.trafficexpr = None

        with TrafficFlowListReport(self.netprofiler) as report:
            report.run(columns=self.options.columns.split(','),
                       sort_col=self.options.sortby,
                       timefilter=self.timefilter,
                       trafficexpr=self.trafficexpr)
            data = report.get_data()
            legend = [c.label for c in report.get_legend()]

        self.print_data(data, legend)


if __name__ == '__main__':
    TrafficFlowListApp().run()
