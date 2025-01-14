#!/usr/bin/env python3
"""
make-deploy-notes.py
====================

Create a wiki-formatted changelog using gitiles and the make-wmf-branch
config.json.

Copyright © 2018 Tyler Cipriani

This program is free software; you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation; either version 2 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License along
with this program; if not, write to the Free Software Foundation, Inc.,
51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
http://www.gnu.org/copyleft/gpl.html
"""

import argparse
import json
import os
import re

from mwrelease.branch import get_bundle
import requests

TOTALS = {
    'changes': 0,
    'repos': 0,
    'unique_authors': set(),
}
GITILES_URL = 'https://gerrit.wikimedia.org/g'
NO_CHANGES = set()

# Messages we don't want to see in the git log
SKIP_MESSAGES = [
    # Fix for escaping fail leaving a commit summary of $COMMITMSG
    'COMMITMSG',
    r'Add (\.gitreview( and )?)?\.gitignore',
    # Branching commit; set version, defaultbranch, add submodules
    'Creating new WMF',
    # git submodule autobumps
    r'Updated mediawiki\/core',
]
SKIP_AUTHORS = [
    'l10n-bot@translatewiki.net',
    # In mediawiki-related repos, this updates development dependencies
    # which don't affect source code.
    'tools.libraryupgrader@tools.wmflabs.org',
]

ALL_CHANGE_SHA1S = []


def flatten_for_post(h, result=None, kk=None):
    """
    Since phab expects x-url-encoded form post data (meaning each
    individual list element is named). AND because, evidently, requests
    can't do this for me, I found a solution via stackoverflow.

    See also:
    <https://secure.phabricator.com/T12447>
    <https://stackoverflow.com/questions/26266664/requests-form-urlencoded-data/36411923>
    """
    if result is None:
        result = {}

    if isinstance(h, str) or isinstance(h, bool):
        result[kk] = h
    elif isinstance(h, list) or isinstance(h, tuple):
        for i, v1 in enumerate(h):
            flatten_for_post(v1, result, '%s[%d]' % (kk, i))
    elif isinstance(h, dict):
        for (k, v) in h.items():
            key = k if kk is None else "%s[%s]" % (kk, k)
            if isinstance(v, dict):
                for i, v1 in v.items():
                    flatten_for_post(v1, result, '%s[%s]' % (key, i))
            else:
                flatten_for_post(v, result, key)
    return result


class PhabChanges(object):
    def __init__(self, changes):
        self.phab_url = 'https://phabricator.wikimedia.org/api/'

        self.changes = changes
        self.conduit_token = self._get_token()
        self.authors = self.find_authors()
        self.train_task = self.find_train_task()

    def _get_token(self):
        """
        Use the $CONDUIT_TOKEN envvar, fallback to whatever is in ~/.arcrc
        """
        token = None
        token_path = os.path.expanduser('~/.arcrc')
        if os.path.exists(token_path):
            with open(token_path) as f:
                arcrc = json.load(f)
                token = arcrc['hosts'][self.phab_url]['token']

        return os.environ.get('CONDUIT_TOKEN', token)

    def _query_phab(self, method, data):
        """
        Helper method to query phab via requests and return json
        """
        data['api.token'] = self.conduit_token
        data = flatten_for_post(data)
        r = requests.post(
            os.path.join(self.phab_url, method),
            data=data)
        r.raise_for_status()
        return r.json()

    def find_authors(self):
        """
        Get a set of authors from a list of commit sha1s
        """
        commits = self._query_phab(
            'diffusion.querycommits',
            {'names': self.changes}
        )

        commits = commits['result']['data']
        authors = set()
        for commit in commits:
            phid = commits[commit]['authorPHID']
            if phid:
                authors.add(phid)

        return authors

    def find_train_task(self):
        """
        Uses stored query to find oldest open train blocker task returns PHID

        This should be the current train...in theory :)
        """
        task = self._query_phab(
            'maniphest.search',
            {
                'queryKey': '17I5JVZeNrOf',
                'limit': 1
            }
        )
        # print(task['result']['data'][0]['fields']['name'])
        return task['result']['data'][0]['phid']

    def subscribe_authors(self):
        """
        Subscribe patch authors for branch to train task
        """
        self._query_phab(
            'maniphest.edit',
            {
                'transactions': [{
                    'type': 'subscribers.add',
                    'value': self.authors
                }],
                'objectIdentifier': self.train_task
            }
        )


def version_parser(ver):
    """
    Validate our version number formats.
    """
    try:
        return re.match(r"(1\.\d\d\.\d+-wmf\.\d+|master)", ver).group(0)
    except re.error:
        raise argparse.ArgumentTypeError(
            "Branch '%s' does not match required format" % ver)


def gitiles_changelog_url(old_branch, new_branch, repo, start=None):
    """
    Create url for valid git log
    """
    url = '{}/{}/+log/{}..{}?format=JSON&no-merges'.format(
        GITILES_URL,
        repo,
        old_branch,
        new_branch
    )

    if start:
        url = '{}&s={}'.format(url, start)

    return url


def patch_url(change):
    """
    Create patch url for gitiles
    """
    ALL_CHANGE_SHA1S.append(change)
    return '{{git|%s}}' % change[:8]


def _git_log_json(old, new, repo, start=None):
    """
    Fetches and loads the json git log from gitiles
    """
    req = requests.get(gitiles_changelog_url(old, new, repo, start))

    if req.status_code != 200:
        if req.status_code == 404:
            return {'log': []}
        raise requests.exceptions.HTTPError(req)

    log_json = req.text
    # remove )]}' since because gerrit.
    return json.loads(log_json[4:])


def git_log(old, new, repo):
    """
    Parses gitlog json from gitiles
    """
    log_json = _git_log_json(old, new, repo)
    changes = log_json['log']
    next_start = log_json.get('next')
    while next_start:
        log_json = _git_log_json(old, new, repo, start=next_start)
        changes += log_json['log']
        next_start = log_json.get('next')

    next_start = None
    return changes


def maybe_task(message):
    """
    Tries to dig a task id out of a commit
    """
    task = ''
    for line in message.splitlines():
        if not line.startswith('Bug: '):
            continue

        task += ' ({{phabricator|%s}})' % line[len('Bug: '):]

    return task


def valid_change(message, author_email):
    """
    validates a change based on a commit
    """
    for skip_message in SKIP_MESSAGES:
        if re.search(skip_message, message):
            return False

    if author_email in SKIP_AUTHORS:
        return False

    return True


def format_changes(old, new, repo):
    """
    format all valid changes
    """
    valid_changes = []

    changes = git_log(old, new, repo)

    for change in changes:
        author_email = change['author']['email']

        if not valid_change(change['message'], author_email):
            continue

        link = patch_url(change['commit'].strip())
        author_name = change['author']['name'].strip()

        TOTALS['unique_authors'].add(author_name)

        message = change['message'].splitlines()[0].strip()

        formatted_change = '* {} - <nowiki>{}</nowiki>{} by {}'.format(
            link, message, maybe_task(change['message']), author_name)

        valid_changes.append(formatted_change)

    TOTALS['changes'] += len(valid_changes)

    if valid_changes:
        TOTALS['repos'] += 1

    return '\n'.join(valid_changes)


def print_formatted_changes(old, new, extension, header, display_name=None):
    """
    Print our changes if there are any, otherwise store the repository name for later use
    """
    if not display_name:
        display_name = extension

    changes = format_changes(old, new, extension)
    if changes:
        print(header)
        print(changes)
    else:
        NO_CHANGES.add(display_name)


def parse_args():
    """
    Parse arguments
    """
    argp = argparse.ArgumentParser()
    argp.add_argument(
        'oldbranch',
        metavar='OLD BRANCH',
        type=version_parser,
        help='Old branch (e.g., 1.31.0-wmf.23)'
    )
    argp.add_argument(
        'newbranch',
        metavar='NEW BRANCH',
        type=version_parser,
        help='New branch (e.g., 1.31.0-wmf.24)'
    )
    argp.add_argument(
        '--add-commiters',
        action='store_true',
        help='Add commiters to task'
    )
    return argp.parse_args()


def main():
    """
    Entry point
    """
    args = parse_args()
    old = os.path.join('wmf', args.oldbranch)
    new = os.path.join('wmf', args.newbranch)

    print_formatted_changes(old, new, 'mediawiki/core', '== Core changes ==')
    print_formatted_changes(old, new, 'mediawiki/vendor', '=== Vendor ===')

    repos = get_bundle('wmf_branch')
    printed_skins = False
    printed_misc = False

    # The below relies on get_bundle returning repos in the following order:
    # - mediawiki/extensions/*
    # - mediawiki/skins/*
    # - misc repos (e.g. mediawiki/vendor, and VisualEditor/VisualEditor)
    print("== Extensions ==")
    for repo_full_name in repos:
        # We already did vendor
        if repo_full_name == 'mediawiki/vendor':
            continue

        repo_is_ext = repo_full_name.startswith('mediawiki/extensions/')
        repo_is_skin = repo_full_name.startswith('mediawiki/skins/')
        repo_is_misc = not (repo_is_ext or repo_is_skin)
        repo_display_name = repo_full_name
        if repo_is_ext or repo_is_skin:
            repo_display_name = os.path.basename(repo_full_name)

        # Print a header at the start
        if repo_is_skin and not printed_skins:
            printed_skins = True
            print('== Skins ==')

        # Print a skin header at the start
        if repo_is_misc and not printed_misc:
            printed_misc = True
            print('== Misc ==')

        print_formatted_changes(old, new, repo_full_name,
                                '=== {} ==='.format(repo_display_name),
                                display_name=repo_display_name)

    if len(NO_CHANGES) > 0:
        print('== No changes ==')
        for component in sorted(NO_CHANGES):
            print('* {}'.format(component))

    print("== Total changes ==\n"
          "'''{}''' Changes "
          "in '''{}''' repos "
          "by '''{}''' authors".format(
              TOTALS['changes'],
              TOTALS['repos'],
              len(TOTALS['unique_authors'])))

    if args.add_commiters:
        phab_changes = PhabChanges(ALL_CHANGE_SHA1S)
        phab_changes.subscribe_authors()


if __name__ == '__main__':
    main()
