import panoply
import conf
import copy
import time
import tempfile
import shutil
import zipfile
import csv
from functools import partial
from itertools import chain
from mandrill import Mandrill
from urllib2 import urlopen

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR
DAY_RANGE = conf.DAY_RANGE
DESTINATION = "mandrill_{type}"
IDPATTERN = "{time}-{key}-{type}-{name}-{address}-{url}"
SLEEP_TIME_SECONDS = 20
COPY_CHUNK_SIZE = 16 * 1024
CSV_FILE_NAME = "activity.csv"

def mergeDicts(x, y):
    '''Given two dicts, merge them into a new dict as a shallow copy.'''
    z = x.copy()
    z.update(y)
    return z

def reportProgress(fn):
    '''decorator for auto progress report'''
    def wrapper(self, *args, **kwargs):
        result = fn(self, *args, **kwargs)
        loaded = self.total - len(self.metrics)
        msg = "%s of %s metrics loaded" % (loaded, self.total)
        self.progress(loaded, self.total, msg)
        return result
    return wrapper

def formatTime(struct_time, format="%Y-%m-%d"):
    return time.strftime(format, struct_time)

class PanoplyMandrill(panoply.DataSource):

    def __init__(self, source, opt):
        super(PanoplyMandrill, self).__init__(source, opt)

        source["destination"] = source.get("destination") or DESTINATION
        source["idpattern"] = source.get("idpattern") or IDPATTERN

        fromsec = int(time.time() - (DAY_RANGE * DAY))
        self.fromTime, self.toTime = formatTime(time.gmtime(fromsec)), formatTime(time.gmtime())
        self.metrics = copy.deepcopy(conf.metrics)
        self.total = len(self.metrics)
        self.key = source.get('key')
        self.mandrill_client = Mandrill(self.key)
        # will raise InvalidKeyError if the api key is wrong
        self.mandrill_client.users.ping()

    @reportProgress
    def read(self, n = None):
        if len(self.metrics) == 0:
            return None # No more data to consume
        metric = self.metrics[0]

        # choose the right handler for this metric
        required_field = metric.get("required")
        handler = lambda: None
        if required_field:
            handler = partial(self.handleRequired, metric, required_field)
        elif metric.get('category') == 'exports':
            handler = partial(self.handleExport, metric)
        else:
            handler = partial(self.handleRegular, metric)

        result = handler()
        # add type and key to each row
        result = [dict(type=metric["name"], key=self.key, **row) for row in result]
        self.metrics.pop(0)
        return result
    
    def getFn(self, metric, path=None):
        '''dynamically locate the right function to call from the sdk.'''
        fn = getattr(getattr(self.mandrill_client, metric['category']), path or metric['path'])
        if metric.get('includeTimeframe'):
            fn = partial(fn, date_from=self.fromTime, date_to=self.toTime)
        return fn
    
    def handleRequired(self, metric, required_field):
        '''for metrics that would need an extra api call before they can work.'''
        list_fn = self.getFn(metric, 'list')
        # extract only the required field from each object in the result array
        extracted_fields = [row.get(required_field) for row in list_fn() if row.get(required_field)]
        fn = self.getFn(metric)
        # for each field we have (for example each email we got from the list call)
        # do an api call on that field
        # for example mandrill_client.senders.time_series(address='someUnique@email.com')
        results = []
        for field in extracted_fields:
            # dynamically choose the paramater to send to the function
            param_dict = {required_field: field}
            # the response from the api call contains an array, we need to add some info
            # on each of the result objects inside this array
            # the info is the param dict itself (for example adding address: 'blabla@a.a')
            result = [mergeDicts(param_dict, response_obj) for response_obj in fn(**param_dict)]
            results.append(result)
        # flatten the results (which are a list of lists) into flat list
        return list(chain.from_iterable(results))
    
    def handleRegular(self, metric):
        '''for your everyday metric.'''
        return self.getFn(metric)()

    def handleExport(self, metric):
        '''for export metrics'''
        fn = self.getFn(metric)
        args = {
            "notify_email": "kfir@panoply.io"
        }
        #job_info = fn(**args)
        url = 'https://s3-us-west-2.amazonaws.com/exports.mandrillapp.com/30874770/activity-2016-12-04_09%3A43%3A42.zip?AWSAccessKeyId=AKIAIQ6QCLKVCWDPU5IA&Expires=1481449502&Signature=bIYQOXI3SOBp%2Fi95522WbsAZdQU%3D'

        # TODO: add an auto stop after some hours
        @reportProgress
        def wait_for_job(self):
            '''
            Report progress is expecting self to be the first param.
            Will return the url on success or False on fail
            '''
            job_status = self.mandrill_client.exports.info(id=job_info.get('id'))
            if (job_status.get('result_url')):
                return job_status.get('result_url')
            status = job_status.get('status')
            if (status == 'error' or status == 'expired'):
                self.log('WARN: export job status was:', status);
                return False
            time.sleep(SLEEP_TIME_SECONDS)

        while url is None:
            url = wait_for_job(self)
        # check that we didn't fail
        if url == False:
            return []
        
        # now we have the url to download from
        req = urlopen(url)
        results = []
        tmp_file = tempfile.NamedTemporaryFile(delete=True)
        try:
            shutil.copyfileobj(req, tmp_file, COPY_CHUNK_SIZE)
            zf = zipfile.ZipFile(tmp_file)
            csv_reader = csv.DictReader(zf.open(CSV_FILE_NAME), delimiter=',')
            for row in csv_reader:
                results.append(row)
        finally:
            tmp_file.close()
        self.log('results length:', len(results))
        self.log('example result line:', results[2])
        time.sleep(30)
        return results
