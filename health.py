"""health.py
Test of new community health API.
"""
import json

import gitdata as gd

gd.auth_config({'username': 'msftgits'})
HEADERS_DICT = {"Accept": "application/vnd.github.black-panther-preview+json"}

print('org,repo,id,health_percentage,code_of_conduct,license,has_readme,has_contributing')

for line in open('temp.csv', 'r').readlines():
    values = line.strip().split(',')
    repo = values[0]
    org = values[1]
    repoid = values[2]

    ENDPOINT = '/repositories/' + repoid + '/community/profile'
    RESPONSE = gd.github_api(endpoint=ENDPOINT, auth=gd.auth_user(), headers=HEADERS_DICT)
    JSONDATA = json.loads(RESPONSE.text)

    print(org + ',' + repo + ',' + repoid + ',' + \
        str(JSONDATA.get('health_percentage', 0)) + ',' + \
        str(JSONDATA.get('code_of_conduct', 'None')) + ',' + \
        str(JSONDATA.get('license', 'None')) + ',' + \
        str(JSONDATA.get('has_readme', 'False')) + ',' + \
        str(JSONDATA.get('has_contributing', 'False')))
