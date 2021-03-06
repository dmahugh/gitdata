"""GitData 2.0 - GitHub query CLI

Entry point:
cli() --------------------> Handle command-line arguments.
"""
import collections
import configparser
import json
import os
import sys
import time
from timeit import default_timer

import click

from dougerino import dicts2csv, dicts2json, setting, time_stamp, logcalls
from githuberino import github_allpages

CONTEXT_SETTINGS = dict(help_option_names=['-h', '--help'])
@click.group(context_settings=CONTEXT_SETTINGS, options_metavar='[options]',
             invoke_without_command=True)
@click.option('-a', '--auth', default='',
              help='GitHub username (for configuring access)', metavar='<str>')
@click.option('-t', '--token', default='',
              help='store access token for specified username', metavar='<str>')
@click.option('-d', '--delete', default=False,
              help='delete specified username', is_flag=True, metavar='')
@click.version_option(version='1.0', prog_name='Gitdata')
@click.pass_context
def cli(ctx, auth, token, delete): #-----------------------------------------<<<
    """\b
------------------------------------
Get information from GitHub REST API
------------------------------------
syntax help: gitdata <subcommand> -h"""
    if auth:
        auth_status(auth.lower(), token, delete)
        return

    # note that all subcommands are invoked by the Click framework decorators,
    # so nothing to do here.

class _settings: #-----------------------------------------------------------<<<
    """This class exists to provide a namespace used for global settings.
    Use auth_config() or log_config() to change these settings.
    """

    # authentication settings used by auth_*() functions
    username = '' # default = no GitHub authentication
    accesstoken = '' # auth_config() may set this from '../_private' folder

    datasource = 'p' # a=API, c=cache, p=prompt user to select

    # current session object from requests library
    requests_session = None

    verbose = False # whether to display status information on console
    display_data = True # whether to display retrieved data on console

    # initialize gitdata session settings
    start_time = time.time() # session start time (seconds)
    tot_api_calls = 0 # number of API calls made through gitdata
    tot_api_bytes = 0 # total bytes returned by these API calls
    last_ratelimit = 0 # API rate limit for the most recent API call
    last_remaining = 0 # remaining portion of rate limit after last API call

    unknownfieldname = set() # list of unknown field names encountered

def auth_config(settings=None): #--------------------------------------------<<<
    """Configure authentication settings.

    1st parameter = dictionary of configuration settings; see config_settings
                    below for settings managed by this function.

    Returns dictionary of current settings - call auth_config() with no
    parameters to get status.
    """
    config_settings = ['username', 'accesstoken']

    # if username is specified but no accesstoken specified, look up this
    # user's PAT setting()
    if settings and 'username' in settings and not 'accesstoken' in settings:
        if not settings['username']:
            settings['accesstoken'] = None
        else:
            settings['accesstoken'] = setting('github', settings['username'], 'pat')
            if not settings['accesstoken']:
                click.echo('Unknown authentication username: ' +
                           settings['username'])

    if settings:
        for settings_opt in config_settings:
            if settings_opt in settings:
                setattr(_settings, settings_opt, settings[settings_opt])

    retval = dict()
    for settings_opt in config_settings:
        retval[settings_opt] = getattr(_settings, settings_opt)

    return retval

def auth_status(auth, token, delete): #--------------------------------------<<<
    """Display status for a GitHub username.

    auth   = username
    token  = optional GitHub access token; if provided, the existing token in
             the INI file is replaced with this value.
    delete = flag for whether to delete the username from INI file
    """
    if token or delete:
        # both of these options write to the file, so initialize parser
        configfile = inifile_name()
        config = configparser.ConfigParser()
        config.read(configfile)
        if delete:
            config.remove_section(auth)
        else:
            # save this token; may need to create a new section
            if not auth in config.sections():
                config[auth] = {}
            config[auth]['PAT'] = token
        with open(configfile, 'w') as fhandle:
            config.write(fhandle)

    # display username and access token
    click.echo('  Username: ' + auth)
    click.echo('     Token: ' + token_abbr(setting('github', auth, 'pat')))

def auth_user(): #-----------------------------------------------------------<<<
    """Credentials for basic authentication.

    Returns the tuple used for API calls, based on current settings.
    Returns None if no GitHub username/PAT is currently set.
    <internal>
    """
    if _settings.username:
        return (_settings.username, _settings.accesstoken)

    return None

def cache_exists(endpoint, auth=None): #-------------------------------------<<<
    """Check whether cached data exists for an endpoint.

    endpoint = GitHub REST API endpoint
    auth = GitHub authentication username

    Returns True if local cached data exists, False if not.
    """
    return os.path.isfile(cache_filename(endpoint, auth))

def cache_filename(endpoint, auth=None): #-----------------------------------<<<
    """Get cache filename for specified user/endpoint.

    endpoint = the endpoint at https://api.github.com (starts with /)
    auth = authentication username

    Returns the filename for caching data returned from this API call.
    """
    if not auth:
        auth = _settings.username if _settings.username else '_anon'

    source_folder = os.path.dirname(os.path.realpath(__file__))
    filename = auth + '_' + endpoint.replace('/', '-').strip('-')
    if '?' in filename:
        # remove parameters from the endpoint
        filename = filename[:filename.find('?')]

    return os.path.join(source_folder, 'gh_cache/' + filename + '.json')

def cache_update(endpoint, payload, constants): #----------------------------<<<
    """Update cached data.

    endpoint  = the API endpoint (e.g., '/repos/org')
    payload   = the list of dictionaries returned from API endpoint
    constants = dictionary of fieldnames/values to be included in the
                cached data (e.g., criteria used in the API call)

    Writes the cache file for this endpoint. Overwrites existing cached data.
    """

    if constants:
        # add the constants to the API payload
        cached_data = []
        for data_item in payload:
            for fldname in constants:
                data_item[fldname] = constants[fldname]
            cached_data.append(data_item)
    else:
        cached_data = payload # no constants to be added

    filename = cache_filename(endpoint)
    dicts2json(source=payload, filename=filename) # write cached data

    if _settings.verbose:
        nameonly = os.path.basename(filename)
        click.echo('Cache update: ', nl=False)
        click.echo(click.style(nameonly, fg='cyan'))

@cli.command(help='Get collaborator information for a repo')
@click.option('-o', '--owner', default='',
              help='owner (org or user)', metavar='<str>')
@click.option('-r', '--repo', default='',
              help='repo name', metavar='<str>')
@click.option('--audit2fa', is_flag=True,
              help='include only 2FA-not-enabled collaborators')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def collabs(owner, repo, audit2fa, authuser, source, #-----------------------<<<
            filename, fields, display, verbose, listfields):
    """Get collaborator information for a repo.
    """
    if listfields:
        list_fields('collab') # display online help
        return

    # validate inputs/options
    if not owner or not repo:
        click.echo('ERROR: must specify owner and repo')
        return
    if not filename_valid(filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    endpoint = '/repos/' + owner + '/' + repo + '/collaborators?per_page=100' + \
        ('&filter=2fa_disabled' if audit2fa else '')
    templist = github_data(
        endpoint=endpoint, entity='collab',
        fields=fldnames, constants={"owner": owner, "repo": repo}, headers={})

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

@cli.command(help='Get commits for a repo')
@click.option('-o', '--owner', default='',
              help='owner (org or user)', metavar='<str>')
@click.option('-r', '--repo', default='',
              help='repo name', metavar='<str>')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def commits(owner, repo, authuser, source, filename, fields, #---------------<<<
            display, verbose, listfields):
    """Get commits for a repo.
    """
    if listfields:
        list_fields('commit') # display online help
        return

    # validate inputs/options
    if not owner or not repo:
        click.echo('ERROR: must specify owner and repo')
        return
    if not filename_valid(filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    endpoint = '/repos/' + owner + '/' + repo + '/commits?per_page=100'
    templist = github_data(
        endpoint=endpoint, entity='commit',
        fields=fldnames, constants={"owner": owner, "repo": repo}, headers={})

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

def data_fields(*, entity=None, jsondata=None, #-----------------------------<<<
                fields=None, constants=None):
    """Get dictionary of desired values from GitHub API JSON payload.

    entity   = entity type ('repo', 'member')
    jsondata = a JSON payload returned by the GitHub API
    fields   = list of names of fields (entries) to include from the JSON data,
               or one of these shorthand values:
               '*' -------> return all fields returned by GitHub API
               'nourls' --> return all non-URL fields (not *_url or url)
               'urls' ----> return all URL fields (*_url and url)
    constants = dictionary of fieldnames/values that can be included in the
                specified fields but aren't returned by GitHub API (these are
                typically criteria used in the API call)

    Returns a dictionary of fieldnames/values.
    """

    if not fields:
        fields = default_fields(entity)

    values = collections.OrderedDict()

    if fields[0] in ['*', 'urls', 'nourls']:
        # special cases to return all fields or all url/non-url fields
        if constants and fields[0] in ['*', 'nourls']:
            values.update(constants)
        for fldname in jsondata:
            if fields[0] == '*' or \
                (fields[0] == 'urls' and fldname.endswith('url')) or \
                (fields[0] == 'nourls' and not fldname.endswith('url')):
                this_item = jsondata[fldname]
                if str(this_item.__class__) == "<class 'dict'>" and \
                    fields[0] == 'nourls':
                    # this is an embedded dictionary, so for the 'nourls' case
                    # remove *url fields ...
                    values[fldname] = {key:value for
                                       (key, value) in this_item.items()
                                       if not key.endswith('url')}
                else:
                    values[fldname] = this_item
    else:
        # fields == an actual list of fieldnames, not a special case
        for fldname in fields:
            if constants and fldname in constants:
                values[fldname] = constants[fldname]
            else:
                values[fldname.replace('.', '_')] = \
                    nested_json_value(jsondata, fldname)
                if fldname.lower() == 'private':
                    values[fldname] = \
                        'private' if jsondata[fldname] else 'public'
    return values

def data_display(datasource=None): #-----------------------------------------<<<
    """Display data on console.

    datasource   = list of dictionaries

    If _settings.display_data, displays the data in console output
    """
    if not _settings.display_data:
        return

    for data_item in datasource:
        values = [str(value) for _, value in data_item.items()]
        click.echo(click.style(','.join(values), fg='cyan'))

    # List unknown field names encountered in this session (if any)
    try:
        if _settings.unknownfieldname:
            click.echo('Unknown field name(s): ' + \
                ','.join(_settings.unknownfieldname))
    except AttributeError:
        # no unknown fields have been logged
        pass

def data_sort(datadict): #---------------------------------------------------<<<
    """Sort function for output lists.

    takes an OrderedDict object as input, returns lower-case version of the
    first value in the OrderedDict, for use as a sort key.
    """
    sortkey = list(datadict.keys())[0]
    sortvalue = str(datadict[sortkey]).lower()
    return sortvalue

def data_write(filename=None, datasource=None): #----------------------------<<<
    """Write output file.

    filename   = output filename
    datasource = list of dictionaries
    """
    if not filename:
        return

    _, file_ext = os.path.splitext(filename)

    if file_ext.lower() == '.json':
        dicts2json(source=datasource, filename=filename) # write JSON file
    else:
        dicts2csv(datasource, filename) # write CSV file

    click.echo('Output file written: ' + filename)

def default_fields(entity=None): #-------------------------------------------<<<
    """Get default field names for an entity.

    entity = the entity/data type (e.g., "team" or "repo")

    Returns a list of the default field names for this entity.
    """
    if entity == 'member':
        return ['login', 'id', 'type']
    elif entity == 'repo':
        return ['name', 'owner.login']
    elif entity == 'team':
        return ['name', 'id', 'privacy', 'permission']
    elif entity == 'org':
        return ['login', 'user']
    elif entity == 'collab':
        return ['login', 'owner', 'repo', 'id']
    elif entity == 'commit':
        return ['commit.committer.date', 'committer.login', 'commit.message']
    return ['name'] # if unknown entity type, use name

def elapsed_time(starttime): #-----------------------------------------------<<<
    """Display elapsed time.

    starttime    = time to measure from, as returned by default_timer()

    If _settings.verbose, displays elapsed time in seconds.
    """
    if _settings.verbose:
        click.echo('Elapsed time: ', nl=False)
        elapsed = default_timer() - starttime
        click.echo(click.style("{0:.2f}".format(elapsed) + ' seconds', fg='cyan'))

def filename_valid(filename=None): #-----------------------------------------<<<
    """Check filename for valid file type.

    filename = output filename passed on command line

    Returns True if valid, False if not.
    """
    if not filename:
        return True # filename is optional

    _, file_ext = os.path.splitext(filename)
    if file_ext.lower() not in ['.csv', '.json']:
        click.echo('ERROR: output file must be .CSV or .JSON')
        return False

    return True

def github_data(*, endpoint=None, entity=None, fields=None, #----------------<<<
                constants=None, headers=None):
    """Get data for specified GitHub API endpoint.
    endpoint     = HTTP endpoint for GitHub API call
    entity       = entity type ('repo', 'member')
    fields       = list of fields to be returned
    constants    = dictionary of fieldnames/values that can be included in the
                   specified fields but aren't returned by GitHub API (these are
                   typically criteria used in the API call)
    headers      = HTTP headers to be included with API call

    Returns a list of dictionaries containing the specified fields.
    Returns a complete data set - if this endpoint does pagination, all pages
    are retrieved and aggregated.
    """
    # _settings.datasource contains one of these three values:
    # 'a' = call the GitHub REST API to get the data
    # 'c' = get data from the locally cached data for this endpoint/username
    # 'p' (or None) = prompt the user for which data to use

    if _settings.datasource == 'c' and not cache_exists(endpoint):
        click.echo('ERROR: cached data requested, but none found.')
        return []

    if _settings.datasource == 'a':
        read_from = 'a'
    elif _settings.datasource == 'c':
        read_from = 'c'
    else:
        # prompt user for which data source to use
        click.echo('    Endpoint: ', nl=False)
        click.echo(click.style(endpoint, fg='cyan'))
        if cache_exists(endpoint):
            filetime = time_stamp(cache_filename(endpoint))
            click.echo(' Cached data: ', nl=False)
            click.echo(click.style(filetime, fg='cyan'))
            read_from = \
                click.prompt('Read from API (a), cache (c) or exit (x)?').lower()
        else:
            click.echo('Cached data not available.')
            read_from = click.prompt('Read from API (a) or exit (x)?').lower()

    if read_from == 'x':
        sys.exit(0)

    if read_from == 'a':
        all_fields = github_allpages(endpoint=endpoint, auth=auth_user(),
                                     headers=headers, state=_settings)
        cache_update(endpoint, all_fields, constants)
    elif read_from == 'c' and cache_exists(endpoint):
        all_fields = github_data_from_cache(endpoint=endpoint)
        if _settings.verbose:
            nameonly = os.path.basename(cache_filename(endpoint))
            click.echo(' Data source: ', nl=False)
            click.echo(click.style(nameonly, fg='cyan'))
    else:
        all_fields = []

    # extract the requested fields and return them
    retval = []
    for json_item in all_fields:
        retval.append(data_fields(entity=entity, jsondata=json_item,
                                  fields=fields, constants=constants))
    return retval

def github_data_from_cache(endpoint=None): #---------------------------------<<<
    """Get data from local cache file.

    endpoint = GitHub API endpoint
    """
    filename = cache_filename(endpoint)
    return read_json(filename)

def inifile_name(): #--------------------------------------------------------<<<
    """Return full name of INI file where GitHub tokens are stored.
    Note that this file is stored in a 'private' subfolder under the parent
    folder of the gitdata module.
    """
    source_folder = os.path.dirname(os.path.realpath(__file__))
    return os.path.join(source_folder, '../_private/github.ini')

def list_fields(entity=None): #----------------------------------------------<<<
    """Display available field names for an entity.

    entity = the entity type (e.g., 'org' or 'team')

    Displays to the console a list of available field names for this entity.
    """
    click.echo('\nDefault fields for ' + entity.upper() + 'S: ', nl=False)
    click.echo(click.style('/'.join(default_fields(entity)), fg='cyan'))
    click.echo(click.style(60*'-', fg='blue'))
    wildcard_fields()

    if entity == 'collab':
        click.echo(click.style('avatar_url'.ljust(27) + 'organizations_url', fg='cyan'))
        click.echo(click.style('events_url'.ljust(27) + 'received_events_url', fg='cyan'))
        click.echo(click.style('followers_url'.ljust(27) + 'repos_url', fg='cyan'))
        click.echo(click.style('following_url'.ljust(27) + 'site_admin', fg='cyan'))
        click.echo(click.style('gists_url'.ljust(27) + 'starred_url', fg='cyan'))
        click.echo(click.style('gravatar_id'.ljust(27) + 'subscriptions_url', fg='cyan'))
        click.echo(click.style('html_url'.ljust(27) + 'type', fg='cyan'))
        click.echo(click.style('id'.ljust(27) + 'url', fg='cyan'))
        click.echo(click.style('login', fg='cyan'))
    elif entity == 'commit':
        click.echo(click.style('comments_url'.ljust(27) +
                               'commit.message', fg='cyan'))
        click.echo(click.style('html_url'.ljust(27) +
                               'commit.tree.sha', fg='cyan'))
        click.echo(click.style('sha'.ljust(27) +
                               'commit.tree.url', fg='cyan'))
        click.echo(click.style('url'.ljust(27) +
                               'commit.url', fg='cyan'))
        click.echo(click.style('author.avatar_url'.ljust(27) +
                               'commit.url', fg='cyan'))
        click.echo(click.style('author.events_url'.ljust(27) +
                               'commit.verification.payload', fg='cyan'))
        click.echo(click.style('author.followers_url'.ljust(27) +
                               'commit.verification.reason', fg='cyan'))
        click.echo(click.style('author.following_url'.ljust(27) +
                               'commit.verification.signature', fg='cyan'))
        click.echo(click.style('author.gists_url'.ljust(27) +
                               'commit.verification.verified', fg='cyan'))
        click.echo(click.style('author.gravatar_id'.ljust(27) +
                               'committer.avatar_url', fg='cyan'))
        click.echo(click.style('author.html_url'.ljust(27) +
                               'committer.events_url', fg='cyan'))
        click.echo(click.style('author.id'.ljust(27) +
                               'committer.followers_url', fg='cyan'))
        click.echo(click.style('author.login'.ljust(27) +
                               'committer.following_url', fg='cyan'))
        click.echo(click.style('author.organizations_url'.ljust(27) +
                               'committer.gists_url', fg='cyan'))
        click.echo(click.style('author.received_events_url'.ljust(27) +
                               'committer.gravatar_id', fg='cyan'))
        click.echo(click.style('author.repos_url'.ljust(27) +
                               'committer.html_url', fg='cyan'))
        click.echo(click.style('author.site_admin'.ljust(27) +
                               'committer.id', fg='cyan'))
        click.echo(click.style('author.starred_url'.ljust(27) +
                               'committer.login', fg='cyan'))
        click.echo(click.style('author.subscriptions_url'.ljust(27) +
                               'committer.organizations_url', fg='cyan'))
        click.echo(click.style('author.type'.ljust(27) +
                               'committer.received_events_url', fg='cyan'))
        click.echo(click.style('author.url'.ljust(27) +
                               'committer.repos_url', fg='cyan'))
        click.echo(click.style('commit.author.date'.ljust(27) +
                               'committer.site_admin', fg='cyan'))
        click.echo(click.style('commit.author.email'.ljust(27) +
                               'committer.starred_url', fg='cyan'))
        click.echo(click.style('commit.author.name'.ljust(27) +
                               'committer.subscriptions_url', fg='cyan'))
        click.echo(click.style('commit.comment_count'.ljust(27) +
                               'committer.type', fg='cyan'))
        click.echo(click.style('commit.committer.date'.ljust(27) +
                               'committer.url', fg='cyan'))
        click.echo(click.style('commit.committer.email'.ljust(27) +
                               'parents.sha', fg='cyan'))
        click.echo(click.style('commit.committer.name'.ljust(27) +
                               'parents.url', fg='cyan'))
    elif entity == 'member':
        click.echo(click.style('id                  avatar_url          ' +
                               'html_url', fg='cyan'))
        click.echo(click.style('login               events_url          ' +
                               'organizations_url', fg='cyan'))
        click.echo(click.style('org                 followers_url       ' +
                               'received_events_url', fg='cyan'))
        click.echo(click.style('site_admin          following_url       ' +
                               'repos_url', fg='cyan'))
        click.echo(click.style('type                gists_url           ' +
                               'starred_url', fg='cyan'))
        click.echo(click.style('url                 gravatar_id         ' +
                               'subscriptions_url', fg='cyan'))
    elif entity == 'org':
        click.echo(click.style('avatar_url', fg='cyan'))
        click.echo(click.style('description', fg='cyan'))
        click.echo(click.style('events_url', fg='cyan'))
        click.echo(click.style('hooks_url', fg='cyan'))
        click.echo(click.style('id', fg='cyan'))
        click.echo(click.style('issues_url', fg='cyan'))
        click.echo(click.style('login', fg='cyan'))
        click.echo(click.style('members_url', fg='cyan'))
        click.echo(click.style('public_members_url', fg='cyan'))
        click.echo(click.style('repos_url', fg='cyan'))
        click.echo(click.style('url', fg='cyan'))
        click.echo(click.style('user', fg='cyan'))
    elif entity == 'repo':
        click.echo(click.style('archive_url         git_tags_url         ' +
                               'open_issues', fg='cyan'))
        click.echo(click.style('assignees_url       git_url              ' +
                               'open_issues_count', fg='cyan'))
        click.echo(click.style('blobs_url           has_downloads        ' +
                               'private', fg='cyan'))
        click.echo(click.style('branches_url        has_issues           ' +
                               'pulls_url', fg='cyan'))
        click.echo(click.style('clone_url           has_pages            ' +
                               'pushed_at', fg='cyan'))
        click.echo(click.style('collaborators_url   has_wiki             ' +
                               'releases_url', fg='cyan'))
        click.echo(click.style('commits_url         homepage             ' +
                               'size', fg='cyan'))
        click.echo(click.style('compare_url         hooks_url            ' +
                               'ssh_url', fg='cyan'))
        click.echo(click.style('contents_url        html_url             ' +
                               'stargazers_count', fg='cyan'))
        click.echo(click.style('contributors_url    id                   ' +
                               'stargazers_url', fg='cyan'))
        click.echo(click.style('created_at          issue_comment_url    ' +
                               'statuses_url', fg='cyan'))
        click.echo(click.style('default_branch      issue_events_url     ' +
                               'subscribers_url', fg='cyan'))
        click.echo(click.style('deployments_url     issues_url           ' +
                               'subscription_url', fg='cyan'))
        click.echo(click.style('description         keys_url             ' +
                               'svn_url', fg='cyan'))
        click.echo(click.style('downloads_url       labels_url           ' +
                               'tags_url', fg='cyan'))
        click.echo(click.style('events_url          language             ' +
                               'teams_url', fg='cyan'))
        click.echo(click.style('fork                languages_url        ' +
                               'trees_url', fg='cyan'))
        click.echo(click.style('forks               master_branch        ' +
                               'updated_at', fg='cyan'))
        click.echo(click.style('forks_count         merges_url           ' +
                               'url', fg='cyan'))
        click.echo(click.style('forks_url           milestones_url       ' +
                               'watchers', fg='cyan'))
        click.echo(click.style('full_name           mirror_url           ' +
                               'watchers_count', fg='cyan'))
        click.echo(click.style('git_commits_url     name', fg='cyan'))
        click.echo(click.style('git_refs_url        notifications_url', fg='cyan'))
        click.echo(click.style(60*'-', fg='blue'))
        click.echo(click.style('license.featured              ' +
                               'owner.login', fg='cyan'))
        click.echo(click.style('license.key                   ' +
                               'owner.organizations_url', fg='cyan'))
        click.echo(click.style('license.name                  ' +
                               'owner.received_events_url', fg='cyan'))
        click.echo(click.style('license.url                   ' +
                               'owner.repos_url', fg='cyan'))
        click.echo(click.style('owner.avatar_url              ' +
                               'owner.site_admin', fg='cyan'))
        click.echo(click.style('owner.events_url              ' +
                               'owner.starred_url', fg='cyan'))
        click.echo(click.style('owner.followers_url           ' +
                               'owner.subscriptions_url', fg='cyan'))
        click.echo(click.style('owner.following_url           ' +
                               'owner.type', fg='cyan'))
        click.echo(click.style('owner.gists_url               ' +
                               'owner.url', fg='cyan'))
        click.echo(click.style('owner.gravatar_id             ' +
                               'permissions.admin', fg='cyan'))
        click.echo(click.style('owner.html_url                ' +
                               'permissions.pull', fg='cyan'))
        click.echo(click.style('owner.id                      ' +
                               'permissions.push', fg='cyan'))
    elif entity == 'team':
        click.echo(click.style('description', fg='cyan'))
        click.echo(click.style('id', fg='cyan'))
        click.echo(click.style('members_url', fg='cyan'))
        click.echo(click.style('name', fg='cyan'))
        click.echo(click.style('org', fg='cyan'))
        click.echo(click.style('permission', fg='cyan'))
        click.echo(click.style('privacy', fg='cyan'))
        click.echo(click.style('repositories_url', fg='cyan'))
        click.echo(click.style('slug', fg='cyan'))
        click.echo(click.style('url', fg='cyan'))

@cli.command(help='Get member information by org or team ID')
@click.option('-o', '--org', default='',
              help='GitHub org (* = all orgs authuser is a member of)', metavar='<str>')
@click.option('-t', '--team', default='',
              help='team ID', metavar='<str>')
@click.option('--audit2fa', is_flag=True,
              help='include only 2FA-not-enabled members')
@click.option('--adminonly', is_flag=True,
              help='include only members with role=admin')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def members(org, team, audit2fa, adminonly, authuser, #----------------------<<<
            source, filename, fields, display, verbose, listfields):
    """Get member info for an organization or team.
    """
    if listfields:
        list_fields('member')
        return

    # validate inputs/options
    if not org and not team:
        click.echo('ERROR: must specify an org or team ID')
        return
    if not filename_valid(filename=filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    templist = membersdata(org=org, team=team, audit2fa=audit2fa,
                           authname=authuser, adminonly=adminonly, fields=fldnames)

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

def membersdata(*, org=None, team=None, fields=None, authname=None, #--------<<<
                audit2fa=False, adminonly=False):
    """Get members for one or more teams or organizations.

    org = organization name
    team = team ID; if provided, org is ignored
    fields = list of field names to be returned; names must be the same as
             returned by the GitHub API (see list_fields()).
    authname = GitHub authentication username; required for org=* syntax

    You must be authenticated via auth_config() as an admin of the org(s) to
    use the audit2fa or adminonly options ...

    audit2fa  = whether to only return members with 2FA disabled.
    adminonly = whether to only return members with role=admin.

    Returns a list of dictionary objects, one per member.
    """
    memberlist = [] # the list of members that will be returned

    if team:
        # get members by team
        memberlist.extend(membersget(team=team, fields=fields))
    else:
        # get members by organization
        if org == '*':
            # handle special org=* syntax: all orgs for this user
            if not authname:
                click.echo('ERROR: -a option required for org=* syntax.')
                return []
            user_orgs = orglist(authname)
            for orgid in user_orgs:
                memberlist.extend( \
                    membersget(org=orgid, fields=fields,
                               audit2fa=audit2fa, adminonly=adminonly))
        else:
            # get members for a single specified organization
            memberlist.extend( \
                membersget(org=org, fields=fields,
                           audit2fa=audit2fa, adminonly=adminonly))

    return memberlist

def membersget(*, org=None, team=None, fields=None, #------------------------<<<
               audit2fa=False, adminonly=False):
    """Get member info for a specified organization. Called by members() to
    aggregate member info for multiple organizations.

    org =          organization ID (ignored if a team is specified)
    team =         team ID
    fields =       list of fields to be returned

    You must be authenticated via auth_config() as an admin of the org(s) to
    use the audit2fa or adminonly options ...

    audit2fa  = whether to only return members with 2FA disabled.
    adminonly = whether to only return members with role=admin.

    Returns a list of dictionaries containing the specified fields.
    <internal>
    """
    if team:
        endpoint = '/teams/' + team + '/members?per_page=100'
    else:
        endpoint = '/orgs/' + org + '/members?per_page=100' + \
            ('&filter=2fa_disabled' if audit2fa else '') + \
            ('&role=admin' if adminonly else '')

    return github_data(endpoint=endpoint, entity='member', fields=fields,
                       constants={"org": org}, headers={})

def nested_json_value(nested_dict, dot_fldname): #---------------------------<<<
    """Return a nested value from a JSON data structure.

    nested_dict = a JSON object, which contains nested dictionaries (nested up
                  to 4 levels deep)
    dot_fldname = a dot-notation reference to a value nested inside the JSON
                  for example, 'commit.committer.date' would return the value
                  nested_dict['commit']['committer']['date']
    """
    depth = dot_fldname.count('.') + 1
    keys = dot_fldname.split('.')
    if depth == 1:
        try:
            retval = nested_dict[dot_fldname]
        except (TypeError, KeyError):
            _settings.unknownfieldname.add(dot_fldname)
            retval = None
    elif depth == 2:
        try:
            retval = nested_dict[keys[0]][keys[1]]
        except (TypeError, KeyError):
            _settings.unknownfieldname.add(dot_fldname)
            retval = None
    elif depth == 3:
        try:
            retval = nested_dict[keys[0]][keys[1]][keys[2]]
        except (TypeError, KeyError):
            _settings.unknownfieldname.add(dot_fldname)
            retval = None
    elif depth == 4:
        try:
            retval = nested_dict[keys[0]][keys[1]][keys[2]][keys[3]]
        except (TypeError, KeyError):
            _settings.unknownfieldname.add(dot_fldname)
            retval = None
    else:
        try:
            retval = nested_dict[keys[0]][keys[1]][keys[2]][keys[3]][keys[4]]
        except (TypeError, KeyError):
            _settings.unknownfieldname.add(dot_fldname)
            retval = None
    return retval

def orglist(authname=None, contoso=False): #---------------------------------<<<
    """Get all orgs for a GitHub user.

    authname = GitHub user name
    contoso  = whether to include orgs named contoso* (to deal with a Microsoft
               specific problem)

    Returns a list of all GitHub organizations that this user is a member of.
    """
    auth_config({'username': authname})
    templist = github_data(endpoint='/user/orgs', entity='org', fields=['login'],
                           constants={"user": authname}, headers={})
    sortedlist = sorted([_['login'].lower() for _ in templist])

    if contoso:
        return sortedlist
    else:
        return [orgname for orgname in sortedlist if not orgname.startswith('contoso')]

@cli.command(help='Get org memberships for a user')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def orgs(authuser, source, filename, fields, #-------------------------------<<<
         display, verbose, listfields):
    """Get organization information.
    """
    if listfields:
        list_fields('org')
        return

    # validate inputs/options
    if not authuser:
        click.echo('ERROR: authentication username is required')
        return
    if not filename_valid(filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    templist = github_data(
        endpoint='/user/orgs', entity='org', fields=fldnames,
        constants={"user": authuser}, headers={})

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

def read_json(filename=None): #----------------------------------------------<<<
    """Read .json file into a Python object.

    filename = the filename
    Returns the object that has been serialized to the .json file (list, etc).
    <internal>
    """
    with open(filename, 'r') as datafile:
        retval = json.loads(datafile.read())
    return retval

@cli.command(help='Get repo information by org or user/owner')
@click.option('-o', '--org', default='',
              help='GitHub org (* = all orgs authuser is a member of)', metavar='<str>')
@click.option('-u', '--user', default='',
              help='GitHub user', metavar='<str>')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def repos(org, user, authuser, source, filename, #---------------------------<<<
          fields, display, verbose, listfields):
    """Get repository information.
    """
    if listfields:
        list_fields('repo')
        return

    # validate inputs/options
    if not org and not user:
        click.echo('ERROR: must specify an org or user')
        return
    if not filename_valid(filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    templist = reposdata(org=org, user=user, fields=fldnames, authname=authuser)

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

def reposdata(*, org=None, user=None, fields=None, authname=None): #---------<<<
    """Get repo information for one or more organizations or users.

    org      = organization; an organization or list of organizations
    user     = username; a username or list of usernames (if org is provided,
               user is ignored)
    fields   = list of fields to be returned; names must be the same as
               returned by the GitHub API (see list_fields()).
               Dot notation for embedded elements is supported. For example,
               pass a field named 'license.name' to get the 'name' element of
               the 'license' entry for each repo.
               These special cases are also supported:
               fields=['*'] -------> return all fields returned by GitHub API
               fields=['nourls'] -> return all non-URL fields (not *_url or url)
               fields=['urls'] ----> return all URL fields (*_url and url)
    authname = GitHub authentication username; required for org=* syntax

    Returns a list of dictionary objects, one per repo.
    """
    repolist = [] # the list of repos that will be returned

    if org:
        # get repos by organization
        if org == '*':
            # handle special org=* syntax: all orgs for this user
            if not authname:
                click.echo('ERROR: -a option required for org=* syntax.')
                return []
            user_orgs = orglist(authname)
            for orgid in user_orgs:
                repolist.extend(reposget(org=orgid, fields=fields))
        else:
            # get repos for specified organization
            repolist.extend(reposget(org=org, fields=fields))
    else:
        # get repos by user
        repolist.extend(reposget(user=user, fields=fields))

    return repolist

def reposget(*, org=None, user=None, fields=None): #-------------------------<<<
    """Get repo information for a specified org or user. Called by repos() to
    aggregate repo information for multiple orgs or users.

    org = organization name
    user = username (ignored if org is provided)
    fields = list of fields to be returned

    Returns a list of dictionaries containing the specified fields.

    NOTE: if authenticated user is same as specified user, the returned data
    will NOT include their private repos. To get private repos, need to use
    the user/repos endpoint (and that includes every repo they have access to,
    in any org, in addition to their own repos)
    <internal>
    """
    if org:
        endpoint = '/orgs/' + org + '/repos?per_page=100'
    else:
        endpoint = '/users/' + user + '/repos?per_page=100'

    # custom header to retrieve license info while License API is in preview
    headers = {'Accept': 'application/vnd.github.drax-preview+json'}

    return github_data(endpoint=endpoint, entity='repo', fields=fields,
                       headers=headers)

@cli.command(help='Get team information for an organization')
@click.option('-o', '--org', default='',
              help='GitHub organization', metavar='<str>')
@click.option('-a', '--authuser', default='',
              help='authentication username', metavar='<str>')
@click.option('-s', '--source', default='p',
              help='data source - a/API, c/cache, or p/prompt', metavar='<str>')
@click.option('-n', '--filename', default='',
              help='output filename (.CSV or .JSON)', metavar='<str>')
@click.option('-f', '--fields', default='',
              help='fields to include', metavar='<str>')
@click.option('-d', '--display', is_flag=True, default=True,
              help="Don't display retrieved data")
@click.option('-v', '--verbose', is_flag=True, default=False,
              help="Display verbose status info")
@click.option('-l', '--listfields', is_flag=True,
              help='list available fields and exit.')
def teams(org, authuser, source, filename, fields, #-------------------------<<<
          display, verbose, listfields):
    """get team information for an organization.
    """
    if listfields:
        list_fields('team')
        return

    # validate inputs/options
    if not org:
        click.echo('ERROR: must specify an org')
        return
    if not filename_valid(filename):
        return

    start_time = default_timer()

    # store settings in _settings
    _settings.display_data = display
    _settings.verbose = verbose
    source = source if source else 'p'
    _settings.datasource = source.lower()[0]

    # retrieve requested data
    auth_config({'username': authuser})
    fldnames = fields.split('/') if fields else None
    templist = github_data(
        endpoint='/orgs/' + org + '/teams?per_page=100', entity='team',
        fields=fldnames, constants={"org": org}, headers={})

    # handle returned data
    sorted_data = sorted(templist, key=data_sort)
    data_display(sorted_data)
    data_write(filename, sorted_data)

    elapsed_time(start_time)

def token_abbr(accesstoken): #-----------------------------------------------<<<
    """Get abbreviated access token (for display purposes).

    Returns an abbreviated version of the passed access token, including only
    the first 2 and last 2 characters.
    """
    if accesstoken:
        return accesstoken[0:2] + '...' + accesstoken[-2:]
    else:
        return "*none*"

def wildcard_fields(): #-----------------------------------------------------<<<
    """Display wildcard field options.
    """
    click.echo(click.style('       specify fields -->  --fields=',
                           fg='white'), nl=False)
    click.echo(click.style('fld1/fld2/etc', fg='cyan'))
    click.echo(click.style('           ALL fields -->  --fields=',
                           fg='white'), nl=False)
    click.echo(click.style('*', fg='cyan'))
    click.echo(click.style('              No URLs -->  --fields=',
                           fg='white'), nl=False)
    click.echo(click.style('nourls', fg='cyan'))
    click.echo(click.style('            Only URLs -->  --fields=',
                           fg='white'), nl=False)
    click.echo(click.style('urls', fg='cyan'))
    click.echo(click.style(60*'-', fg='blue'))

# code to execute when running standalone
if __name__ == '__main__':
    pass
