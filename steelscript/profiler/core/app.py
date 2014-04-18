# Copyright (c) 2013 Riverbed Technology, Inc.
#
# This software is licensed under the terms and conditions of the
# MIT License set forth at:
#   https://github.com/riverbed/flyscript/blob/master/LICENSE ("License").
# This software is distributed "AS IS" as set forth in the License.


from steelscript.common.app import Application
from steelscript.profiler.core import Profiler


class ProfilerApp(Application):
    """Simple class to wrap common command line parsing"""
    def __init__(self, *args, **kwargs):
        super(ProfilerApp, self).__init__(*args, **kwargs)
        self.optparse.set_usage('%prog PROFILER_HOSTNAME <options>')
        self.profiler = None

    def parse_args(self):
        super(ProfilerApp, self).parse_args()
        self.host = self.args[0]

    def setup(self):
        self.profiler = Profiler(self.host,
                                 port=self.options.port,
                                 auth=self.auth)

    def validate_args(self):
        if len(self.args) < 1:
            self.optparse.error('missing PROFILER_HOSTNAME')
