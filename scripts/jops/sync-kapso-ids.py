"""Sync kapsoId from Kapso platform to Twenty CRM workspace members.
Idempotent. Run periodically (e.g. hourly) to catch new members.
"""
import json, time, urllib.request

KAPSO_API_KEY = open('/opt/jops/kapso-api-key.txt').read().strip()
TWENTY_API_KEY = open('/root/.twenty/api_key.txt').read().strip()
TWENTY_API = 'http://localhost:3000/graphql'
KAPSO_USERS_URL = 'https://api.kapso.ai/platform/v1/users'

def kapso_users():
    req = urllib.request.Request(KAPSO_USERS_URL, headers={'X-API-Key': KAPSO_API_KEY})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.load(r).get('data', [])

def twenty_gql(query, variables=None):
    body = {'query': query}
    if variables:
        body['variables'] = variables
    req = urllib.request.Request(
        TWENTY_API, data=json.dumps(body).encode(),
        headers={'Authorization': 'Bearer ' + TWENTY_API_KEY, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.load(r)
    if result.get('errors'):
        raise RuntimeError(json.dumps(result['errors'])[:500])
    return result['data']

# Build email -> kapso_id map from Kapso
email_to_id = {u['email']: u['user_id'] for u in kapso_users() if u.get('email') and u.get('user_id')}
print(f'Kapso users with email: {len(email_to_id)}')

# Query Twenty workspace members
data = twenty_gql('''query {
  workspaceMembers(filter: { deletedAt: { is: NULL } }, first: 100) {
    edges { node { id userEmail kapsoId } }
  }
}''')
members = {n['userEmail']: n for n in [e['node'] for e in data['workspaceMembers']['edges']] if n.get('userEmail')}
print(f'Twenty workspace members: {len(members)}')

# Find members needing update
updated = 0
skipped = 0
errors = 0

for email, m in sorted(members.items()):
    kapso_id = email_to_id.get(email)
    if not kapso_id:
        continue
    current = m.get('kapsoId') or ''
    if current == kapso_id:
        skipped += 1
        continue
    try:
        twenty_gql(
            'mutation UpdateWM($id: ID!, $data: WorkspaceMemberUpdateInput!) { updateWorkspaceMember(id: $id, data: $data) { id kapsoId } }',
            {'id': m['id'], 'data': {'kapsoId': kapso_id}},
        )
        updated += 1
        print(f'  UPDATED {email} -> {kapso_id[:12]}...')
        time.sleep(0.7)
    except Exception as e:
        errors += 1
        print(f'  ERROR {email}: {e}')

print(f'Done: updated={updated} skipped={skipped} errors={errors}')