# JUM-553 Real-Time Sync: Supabase → CRM Webhook Spec

## Architecture

```
Supabase INSERT/UPDATE → Database Trigger → pg_net HTTP call → jumbo-webhook-proxy → CRM GraphQL
```

The webhook proxy at `/opt/jumbo-webhook-proxy/server.js` already handles:
- Rate limiting (token bucket, 80 req/min)
- Async processing queue (max 2 workers)
- Person find/create/update via phone matching
- Buyer find/create/update
- Enquiry creation with zone-based agent assignment

We extend it to handle Supabase-originated events for:
1. **external_user** → person + buyer + enquiry (Phase 3)
2. **visit** → CRM visit (Phase 4)
3. **offer** → CRM opportunity (Phase 5)
4. **external_lead** → CRM enquiry (Phase 6)

## Supabase Side: Database Triggers

### Approach: Supabase Database Webhooks (recommended)

Supabase has built-in Database Webhooks (beta) that fire on INSERT/UPDATE/DELETE
without needing pg_net or custom triggers. Configure via Supabase Dashboard:
- Project Settings → Webhooks → Create webhook per table
- Target URL: `https://<proxy-domain>/api/supabase/<table>`
- Events: INSERT (and UPDATE if needed)
- Secret: shared secret for HMAC verification

### Fallback: pg_net + Trigger Function

If webhooks aren't available on the Supabase plan, use pg_net extension:

```sql
-- Enable extensions
CREATE EXTENSION IF NOT EXISTS pg_net;

-- Generic webhook dispatch function
CREATE OR REPLACE FUNCTION dispatch_supabase_event()
RETURNS trigger AS $$
DECLARE
    payload JSON;
    result INT;
BEGIN
    payload = json_build_object(
        'table', TG_TABLE_NAME,
        'type', TG_OP,
        'record', row_to_json(NEW),
        'old_record', row_to_json(OLD)
    );
    
    result = net.http_post(
        url := 'https://hooks.jumbohomes.in/api/supabase/webhook',
        body := payload::text,
        headers := json_build_object(
            'Content-Type', 'application/json',
            'X-Supabase-Event', TG_OP,
            'X-Supabase-Table', TG_TABLE_NAME
        )
    );
    
    RETURN NEW;
END;
$$ LANGUAGE plpgsql SECURITY DEFINER;
```

### Tables & Triggers

```sql
-- external_user: INSERT only (new website users)
CREATE TRIGGER trg_external_user_sync
    AFTER INSERT ON external_user
    FOR EACH ROW EXECUTE FUNCTION dispatch_supabase_event();

-- visit: INSERT + UPDATE (new visits + status changes)
CREATE TRIGGER trg_visit_sync
    AFTER INSERT OR UPDATE ON visit
    FOR EACH ROW EXECUTE FUNCTION dispatch_supabase_event();

-- offer: INSERT + UPDATE (new offers + status/price changes)
CREATE TRIGGER trg_offer_sync
    AFTER INSERT OR UPDATE ON offer
    FOR EACH ROW EXECUTE FUNCTION dispatch_supabase_event();

-- external_lead: INSERT only (new leads from website)
CREATE TRIGGER trg_external_lead_sync
    AFTER INSERT ON external_lead
    FOR EACH ROW EXECUTE FUNCTION dispatch_supabase_event();
```

## Proxy Side: New Route `/api/supabase/webhook`

Add to `/opt/jumbo-webhook-proxy/server.js`:

```javascript
// --- SUPABASE WEBHOOK SECRET ---
const SUPABASE_WEBHOOK_SECRET = process.env.SUPABASE_WEBHOOK_SECRET || '';

// --- VERIFY SUPABASE SIGNATURE ---
function verifySupabaseSignature(req) {
    if (!SUPABASE_WEBHOOK_SECRET) return true;
    const sig = req.headers['x-supabase-signature'];
    if (!sig) return false;
    const expected = crypto.createHmac('sha256', SUPABASE_WEBHOOK_SECRET)
        .update(req.rawBody).digest('hex');
    return crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
}

// --- SUPABASE EVENT HANDLERS ---

async function handleExternalUserInsert(record) {
    // record: { id, name, phone_number, email, drop_reason, created_at, updated_at, internal_id }
    // idempotent: skip if internal_id already set
    if (record.internal_id) return { success: true, skipped: true, reason: 'already synced' };

    const cleanPhone = normalizePhone(record.phone_number);
    if (!cleanPhone) return { success: false, error: 'invalid phone' };

    // Match existing person by phone
    let person = await findPersonByPhone(cleanPhone);
    if (!person) {
        const { firstName, lastName } = splitName(record.name);
        person = await createPerson({
            name: record.name,
            phoneDigits: cleanPhone,
            email: record.email || null,
        });
        console.log(`[Supabase] Created person ${person.id} for external_user ${record.id}`);
    }

    // Match or create buyer
    let buyer = await findBuyerByPersonId(person.id);
    if (!buyer) {
        buyer = await createBuyer({ name: record.name }, person.id);
        console.log(`[Supabase] Created buyer ${buyer.id} for person ${person.id}`);
    }

    // Create enquiry
    const enquiry = await createEnquiry(
        { sourceDetail: 'WEBSITE' },
        buyer.id, null, null, null,
        record.name, cleanPhone,
        AASHISH_WORKSPACE_MEMBER_ID
    );
    console.log(`[Supabase] Created enquiry ${enquiry.id} for buyer ${buyer.id}`);

    // Write back internal_id to Supabase (via Supabase REST API)
    await updateSupabaseRecord('external_user', record.id, { internal_id: buyer.id });

    return { success: true, person: person.id, buyer: buyer.id, enquiry: enquiry.id };
}

async function handleVisitInsert(record) {
    // record: { id, external_user_id, user_id, listing_id, scheduled_at, status, ... }
    // Map external_user_id → CRM buyer via internal_id
    // Map listing_id → CRM property via internal_id
    
    const externalUserInternalId = await lookupSupabaseInternalId('external_user', record.external_user_id);
    if (!externalUserInternalId) {
        console.log(`[Supabase] Visit ${record.id}: external_user ${record.external_user_id} not yet synced, skipping`);
        return { success: false, error: 'external_user not synced' };
    }

    const listingInternalId = await lookupSupabaseInternalId('listing', record.listing_id);
    
    // Find or create visit in CRM
    const crmBuyerId = externalUserInternalId; // buyer.id IS the internal_id
    const crmPropertyId = listingInternalId; // property.id IS the internal_id

    const visitInput = {
        name: `Visit - ${record.scheduled_at}`,
        buyerProfileId: crmBuyerId,
        propertyId: crmPropertyId || null,
        scheduledAt: record.scheduled_at,
        status: mapVisitStatus(record.status),
    };

    // Check for existing visit by internal_id
    const existingVisit = await gql(
        `query FindVisit($id: UUID) { visits(filter: { internalId: { eq: $id } }, first: 1) { edges { node { id } } } }`,
        { id: record.id }
    );

    if (existingVisit?.visits?.edges?.length > 0) {
        // Update existing
        await gql(`mutation UpdateVisit($id: ID!, $input: VisitUpdateInput!) { updateVisit(id: $id, data: $input) { id } }`,
            { id: existingVisit.visits.edges[0].node.id, input: visitInput });
    } else {
        // Create new
        await gql(`mutation CreateVisit($input: VisitCreateInput!) { createVisit(data: $input) { id } }`,
            { input: visitInput });
    }

    await updateSupabaseRecord('visit', record.id, { internal_id: record.id });
    return { success: true };
}

async function handleOfferInsert(record) {
    // record: { id, external_user_id, user_id, listing_id, offer_price, status, ... }
    const externalUserInternalId = await lookupSupabaseInternalId('external_user', record.external_user_id);
    if (!externalUserInternalId) {
        return { success: false, error: 'external_user not synced' };
    }

    const listingInternalId = await lookupSupabaseInternalId('listing', record.listing_id);

    const offerInput = {
        name: `Offer - ${record.offer_price}`,
        offerAmount: { amountMicros: String(record.offer_price * 1000000), currencyCode: 'INR' },
        buyerId: externalUserInternalId,
        propertyNewId: listingInternalId,
        offerSource: 'WEBSITE',
    };

    // Check for existing opportunity by internal_id
    const existing = await gql(
        `query FindOffer($id: UUID) { opportunities(filter: { internalId: { eq: $id } }, first: 1) { edges { node { id } } } }`,
        { id: record.id }
    );

    if (existing?.opportunities?.edges?.length > 0) {
        await gql(`mutation UpdateOffer($id: ID!, $input: OpportunityUpdateInput!) { updateOpportunity(id: $id, data: $input) { id } }`,
            { id: existing.opportunities.edges[0].node.id, input: offerInput });
    } else {
        await gql(`mutation CreateOffer($input: OpportunityCreateInput!) { createOpportunity(data: $input) { id } }`,
            { input: offerInput });
    }

    await updateSupabaseRecord('offer', record.id, { internal_id: record.id });
    return { success: true };
}

async function handleExternalLeadInsert(record) {
    // record: { id, external_user_id, listing_id, source }
    const externalUserInternalId = await lookupSupabaseInternalId('external_user', record.external_user_id);
    if (!externalUserInternalId) {
        return { success: false, error: 'external_user not synced' };
    }

    const listingInternalId = await lookupSupabaseInternalId('listing', record.listing_id);
    const buyerId = externalUserInternalId;

    // Create enquiry linked to buyer
    const enquiryInput = {
        sourceDetail: mapLeadSource(record.source),
        enquiryType: 'BUY',
        statusDetail: 'NEW_LEAD',
        buyerId: buyerId,
        classifiedListingId: listingInternalId,
    };

    await gql(`mutation CreateEnquiry($input: EnquiryCreateInput!) { createEnquiry(data: $input) { id enquiryNumber } }`,
        { input: enquiryInput });

    await updateSupabaseRecord('external_lead', record.id, { internal_id: record.id });
    return { success: true };
}

// --- LOOKUP HELPER ---
// Maps Supabase UUID → CRM UUID via internal_id columns
const internalIdCache = new Map();
const INTERNAL_ID_CACHE_TTL = 5 * 60 * 1000;

async function lookupSupabaseInternalId(table, supabaseId) {
    if (!supabaseId) return null;
    const cacheKey = `${table}:${supabaseId}`;
    const cached = internalIdCache.get(cacheKey);
    if (cached && (Date.now() - cached.ts) < INTERNAL_ID_CACHE_TTL) {
        return cached.internalId;
    }
    const data = await gql(
        `query LookupInternalId($id: UUID) { ${table}(filter: { id: { eq: $id } }, first: 1) { edges { node { internalId } } } }`,
        { id: supabaseId }
    );
    const internalId = data?.[table]?.edges?.[0]?.node?.internalId;
    if (internalId) internalIdCache.set(cacheKey, { internalId, ts: Date.now() });
    return internalId;
}

// --- SUPABASE REST API HELPER ---
async function updateSupabaseRecord(table, id, updates) {
    const supabaseUrl = process.env.SUPABASE_URL || '';
    const supabaseKey = process.env.SUPABASE_SERVICE_ROLE_KEY || '';
    if (!supabaseUrl || !supabaseKey) return;
    
    await fetch(`${supabaseUrl}/rest/v1/${table}?id=eq.${id}`, {
        method: 'PATCH',
        headers: {
            'apikey': supabaseKey,
            'Authorization': `Bearer ${supabaseKey}`,
            'Content-Type': 'application/json',
        },
        body: JSON.stringify(updates),
    });
}

// --- MAIN WEBHOOK ROUTE ---
app.post('/api/supabase/webhook', async (req, res) => {
    if (!verifySupabaseSignature(req)) return res.status(401).json({ error: 'Invalid signature' });
    
    const { table, type, record } = req.body;
    if (!record?.id) return res.status(400).json({ error: 'Missing record' });

    res.status(202).json({ accepted: true, table, id: record.id });
    
    const job = async () => {
        switch (table) {
            case 'external_user': return type === 'INSERT' ? handleExternalUserInsert(record) : null;
            case 'visit': return (type === 'INSERT' || type === 'UPDATE') ? handleVisitInsert(record) : null;
            case 'offer': return (type === 'INSERT' || type === 'UPDATE') ? handleOfferInsert(record) : null;
            case 'external_lead': return type === 'INSERT' ? handleExternalLeadInsert(record) : null;
            default: return { success: false, error: 'Unknown table' };
        }
    };

    enqueueWork(job)
        .then(result => console.log(`[Supabase] ${table} ${record.id}:`, JSON.stringify(result)))
        .catch(err => console.error(`[Supabase] ${table} ${record.id} failed:`, err.message));
});
```

## Deployment Checklist

1. **Supabase Dashboard**: Create webhooks for each table (or create pg triggers)
2. **Proxy**: Add route + handlers to `server.js`
3. **Environment**: Add `SUPABASE_WEBHOOK_SECRET`, `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` to `.env`
4. **SSL**: Ensure proxy is reachable via HTTPS (Caddy reverse proxy)
5. **Build**: `docker-compose up -d --build`
6. **Test**: Insert a test record in Supabase → verify CRM creation

## Error Handling

- Idempotent: check `internal_id` before processing
- Failed events logged to stdout (visible via `docker logs jumbo-webhook-proxy`)
- No retry on person/enquiry creation conflicts (safe to retry)
- Rate limiting: existing token bucket handles CRM API limits

## Tables NOT in scope

- `building`, `listing`, `listing_inspection`: CRM → Supabase only (one-way)
- `user`: Website-only, separate from external_user sync
- `seller`: Separate flow (Task 9)
