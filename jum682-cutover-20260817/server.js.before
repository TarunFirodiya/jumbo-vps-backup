import 'dotenv/config';
import express from 'express';
import crypto from 'crypto';
import pg from 'pg';

const app = express();
app.use(express.json({ verify: (req, res, buf) => { req.rawBody = buf; } }));

// --- CONFIG ---
const TWENTY_API_URL = process.env.TWENTY_API_URL || 'https://admin.jumbohomes.in/graphql';
const TOKEN = process.env.TOKEN || '';
const PORT = process.env.PORT || 3001;
const KAPSO_WEBHOOK_SECRET = process.env.KAPSO_WEBHOOK_SECRET || '';
// Aashish's workspace member ID — new enquiries auto-assigned to him (Ananya proxy)
const AASHISH_WORKSPACE_MEMBER_ID = '404bdd9e-04c6-4ec6-a913-c9d98ab07c92';
// Ridhima's workspace member ID — web signups reassigned to her after 5 min
const RIDHIMA_WORKSPACE_MEMBER_ID = '59f6d8b8-db59-4af6-9920-eb06a311f496';

// --- SUPABASE WEBHOOK CONFIG ---
const SUPABASE_WEBHOOK_SECRET = process.env.SUPABASE_WEBHOOK_SECRET || '';
// Supabase service role key for writing back internal_id
const SUPABASE_URL = process.env.SUPABASE_URL || '';
const SUPABASE_SERVICE_KEY = process.env.SUPABASE_SERVICE_KEY || '';
// 99acres listing relay. Token and shared secret stay in the proxy environment.
const NINETYNINE_ACRES_TOKEN = process.env.NINETYNINE_ACRES_TOKEN || '';
const NINETYNINE_ACRES_RELAY_SECRET = process.env.NINETYNINE_ACRES_RELAY_SECRET || '';

// Durable source-level deduplication for portal replay protection.
// This is intentionally database-backed: the in-memory work queue is not an
// idempotency boundary and disappears whenever the proxy is recreated.
const { Pool } = pg;
const dedupPool = process.env.PG_DATABASE_URL ? new Pool({ connectionString: process.env.PG_DATABASE_URL, max: 4 }) : null;
const DEDUP_WINDOW_HOURS = 72;
let dedupStoreReady;
let visitRetryStoreReady;
function initVisitRetryStore() {
  if (!dedupPool) return Promise.reject(new Error('RETRY_DATABASE_NOT_CONFIGURED'));
  if (!visitRetryStoreReady) {
    visitRetryStoreReady = dedupPool.query(`
      CREATE TABLE IF NOT EXISTS public.supabase_visit_retry (
        source_id uuid PRIMARY KEY,
        payload jsonb NOT NULL,
        status text NOT NULL DEFAULT 'pending',
        attempts integer NOT NULL DEFAULT 0,
        next_attempt_at timestamptz NOT NULL DEFAULT NOW(),
        last_error text,
        created_at timestamptz NOT NULL DEFAULT NOW(),
        updated_at timestamptz NOT NULL DEFAULT NOW()
      )
    `).catch(err => { visitRetryStoreReady = null; throw err; });
  }
  return visitRetryStoreReady;
}
async function saveVisitRetry(record, error) {
  await initVisitRetryStore();
  await dedupPool.query(`
    INSERT INTO public.supabase_visit_retry (source_id, payload, status, attempts, next_attempt_at, last_error, updated_at)
    VALUES ($1, $2::jsonb, 'pending', 1, NOW() + INTERVAL '1 minute', $3, NOW())
    ON CONFLICT (source_id) DO UPDATE SET
      payload = EXCLUDED.payload,
      status = 'pending',
      attempts = public.supabase_visit_retry.attempts + 1,
      next_attempt_at = NOW() + LEAST(INTERVAL '6 hours', (INTERVAL '1 minute' * POWER(2, LEAST(public.supabase_visit_retry.attempts, 8)))),
      last_error = EXCLUDED.last_error,
      updated_at = NOW()
  `, [record.id, JSON.stringify(record), String(error?.message || error).slice(0, 1000)]);
}
async function markVisitRetryDone(sourceId) {
  if (!dedupPool) return;
  await initVisitRetryStore();
  await dedupPool.query(`UPDATE public.supabase_visit_retry SET status='done', updated_at=NOW() WHERE source_id=$1`, [sourceId]);
}
async function claimVisitRetries() {
  if (!dedupPool) return [];
  await initVisitRetryStore();
  const result = await dedupPool.query(`
    UPDATE public.supabase_visit_retry r SET status='processing', updated_at=NOW()
    WHERE r.source_id IN (
      SELECT source_id FROM public.supabase_visit_retry
      WHERE status='pending' AND next_attempt_at <= NOW()
      ORDER BY next_attempt_at LIMIT 10 FOR UPDATE SKIP LOCKED
    ) RETURNING source_id, payload
  `);
  return result.rows;
}

function initDedupStore() {
  if (!dedupPool) return Promise.reject(new Error('DEDUP_DATABASE_NOT_CONFIGURED'));
  if (!dedupStoreReady) {
    dedupStoreReady = dedupPool.query(`
      CREATE TABLE IF NOT EXISTS public.portal_enquiry_idempotency (
        dedup_key text PRIMARY KEY,
        enquiry_id uuid NOT NULL,
        created_at timestamptz NOT NULL DEFAULT NOW(),
        expires_at timestamptz NOT NULL
      )
    `).catch(err => { dedupStoreReady = null; throw err; });
  }
  return dedupStoreReady;
}
async function reservePortalEnquiry(source, phoneDigits, listingId) {
  if (source !== '99acres' || !listingId) return { duplicate: false, key: null };
  await initDedupStore();
  const key = `99ACRES:${phoneDigits}:${String(listingId).trim().toUpperCase()}`;
  const result = await dedupPool.query(`
    INSERT INTO public.portal_enquiry_idempotency (dedup_key, enquiry_id, expires_at)
    VALUES ($1, gen_random_uuid(), NOW() + ($2 * INTERVAL '1 hour'))
    ON CONFLICT (dedup_key) DO UPDATE
      SET enquiry_id = EXCLUDED.enquiry_id, created_at = NOW(), expires_at = EXCLUDED.expires_at
      WHERE public.portal_enquiry_idempotency.expires_at <= NOW()
    RETURNING dedup_key, enquiry_id
  `, [key, DEDUP_WINDOW_HOURS]);
  return { duplicate: result.rowCount === 0, key };
}
async function releasePortalEnquiry(key) {
  if (key && dedupPool) await dedupPool.query('DELETE FROM public.portal_enquiry_idempotency WHERE dedup_key = $1', [key]);
}
async function commitPortalEnquiry(key, enquiryId) {
  if (key && dedupPool) await dedupPool.query('UPDATE public.portal_enquiry_idempotency SET enquiry_id = $2 WHERE dedup_key = $1', [key, enquiryId]);
}

// --- ZONE RESOLUTION ---
// Building zoneId -> active ZoneAgent allocation -> workspace member
// Initial rollout expects one active allocation per zone. Round-robin is deferred.

async function findZoneAgentForBuilding(buildingId) {
  if (!buildingId) return null;
  try {
    // Step 1: Get the building's zoneId
    const buildingData = await gql(`
      query GetBuildingZone($id: UUID) {
        buildings(filter: { id: { eq: $id } }, first: 1) {
          edges { node { zoneId } }
        }
      }
    `, { id: buildingId });
    const zoneId = buildingData?.buildings?.edges?.[0]?.node?.zoneId;
    if (!zoneId) {
      console.log(`  [Zone] Building ${buildingId} has no zoneId`);
      return null;
    }

    // Step 2: Find the workspace member assigned to this zone
    const agentData = await gql(`
      query GetZoneAgent($zoneId: UUID) {
        zoneAgents(filter: { zoneId: { eq: $zoneId }, name: { startsWith: "JUM682 " }, isactive: { eq: true }, deletedAt: { is: NULL } }, first: 10) {
          edges { node { agentId agent { id name { firstName lastName } } } }
        }
      }
    `, { zoneId });
    const agentEdges = agentData?.zoneAgents?.edges || [];
    if (!agentEdges.length) {
      console.log(`  [Zone] No agent assigned to zone ${zoneId}`);
      return null;
    }
    if (agentEdges.length > 1) {
      console.warn(`  [Zone] Multiple active agents for zone ${zoneId}; using the first allocation until round-robin is enabled`);
    }
    const allocation = agentEdges[0].node;
    const agent = allocation.agent;
    const agentId = allocation.agentId || agent?.id;
    const agentName = agent?.name ? `${agent.name.firstName || ''} ${agent.name.lastName || ''}`.trim() : agentId;
    console.log(`  [Zone] Building ${buildingId} -> zone ${zoneId} -> agent ${agentName} (${agentId})`);
    return { agentId, agentName, zoneId };
  } catch (err) {
    console.error(`  [Zone] Error resolving zone for building ${buildingId}:`, err.message);
    return null;
  }
}

// --- ENQUIRY REASSIGNMENT QUEUE ---
// Enquiries that need to be reassigned from Aashish to zone agent after 5 minutes
const REASSIGNMENT_QUEUE = [];
const REASSIGNMENT_DELAY_MS = 5 * 60 * 1000; // 5 minutes

async function processReassignmentQueue() {
  const now = Date.now();
  while (REASSIGNMENT_QUEUE.length > 0 && REASSIGNMENT_QUEUE[0].reassignAt <= now) {
    const item = REASSIGNMENT_QUEUE.shift();
    try {
      // Direct assignment (e.g. web signups -> Ridhima) bypasses zone lookup
      if (item.targetAgentId) {
        await gql(`
          mutation ReassignEnquiry($id: ID!, $input: EnquiryUpdateInput!) {
            updateEnquiry(id: $id, data: $input) { id assignedAgent { id } }
          }
        `, { id: item.enquiryId, input: { assignedAgentId: item.targetAgentId } });
        console.log(`  [Reassign] Enquiry ${item.enquiryId} reassigned from Aashish to ${item.targetAgentName || item.targetAgentId}`);
      } else {
        const zoneAgent = await findZoneAgentForBuilding(item.buildingId);
        if (zoneAgent) {
          await gql(`
            mutation ReassignEnquiry($id: ID!, $input: EnquiryUpdateInput!) {
              updateEnquiry(id: $id, data: $input) { id assignedAgent { id } }
            }
          `, { id: item.enquiryId, input: { assignedAgentId: zoneAgent.agentId } });
          console.log(`  [Reassign] Enquiry ${item.enquiryId} reassigned from Aashish to ${zoneAgent.agentName}`);
        } else {
          console.log(`  [Reassign] Enquiry ${item.enquiryId} — no zone agent found, keeping with Aashish`);
        }
      }
    } catch (err) {
      console.error(`  [Reassign] Failed for enquiry ${item.enquiryId}:`, err.message);
    }
  }
}

// Run reassignment checker every 30 seconds
setInterval(processReassignmentQueue, 30000);

// --- TOKEN BUCKET RATE LIMITER ---
const RATE_LIMIT = 80;
const REFILL_RATE = 80 / 60000;
let tokens = RATE_LIMIT;
let lastRefill = Date.now();

function refillTokens() {
  const now = Date.now();
  const elapsed = now - lastRefill;
  const newTokens = elapsed * REFILL_RATE;
  tokens = Math.min(RATE_LIMIT, tokens + newTokens);
  lastRefill = now;
}

async function acquireToken() {
  const deadline = Date.now() + 10000;
  while (true) {
    refillTokens();
    if (tokens >= 1) { tokens -= 1; return; }
    if (Date.now() > deadline) throw new Error('Rate limiter: waited 10s for token, giving up');
    const waitMs = Math.ceil((1 - tokens) / REFILL_RATE);
    await new Promise(r => setTimeout(r, Math.min(waitMs, 500)));
  }
}

// --- ASYNC PROCESSING QUEUE ---
const WORK_QUEUE = [];
let activeWorkers = 0;
const MAX_WORKERS = 2;
const REQUEST_TIMEOUT_MS = 25000;

let stats = { accepted: 0, completed: 0, failed: 0, timedOut: 0, rateLimitErrors: 0, rateLimitPct: 0 };

async function processWorkQueue() {
  while (activeWorkers < MAX_WORKERS && WORK_QUEUE.length > 0) {
    const { work, resolve, reject } = WORK_QUEUE.shift();
    activeWorkers++;
    try { const result = await work(); resolve(result); }
    catch (err) { reject(err); }
    finally { activeWorkers--; setImmediate(processWorkQueue); }
  }
}

function enqueueWork(work) {
  return new Promise((resolve, reject) => {
    const timeout = setTimeout(() => { stats.timedOut++; reject(new Error('Processing timed out')); }, REQUEST_TIMEOUT_MS);
    WORK_QUEUE.push({
      work: () => work().finally(() => clearTimeout(timeout)),
      resolve: (v) => { clearTimeout(timeout); resolve(v); },
      reject: (e) => { clearTimeout(timeout); reject(e); },
    });
    if (activeWorkers < MAX_WORKERS) setImmediate(processWorkQueue);
  });
}

// --- GQL with token bucket + smart retry ---
async function gql(query, variables = {}, retries = 3) {
  for (let attempt = 1; attempt <= retries; attempt++) {
    try { await acquireToken(); }
    catch (e) { throw new Error('Proxy rate limiter exhausted — too many concurrent requests'); }
    let res;
    try {
      res = await fetch(TWENTY_API_URL, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'Authorization': `Bearer ${TOKEN}` },
        body: JSON.stringify({ query, variables }),
      });
    } catch (networkErr) {
      if (attempt < retries) { await new Promise(r => setTimeout(r, attempt * 500)); continue; }
      throw networkErr;
    }
    const responseText = await res.text();
    const contentType = res.headers.get('content-type') || '';
    let json;
    try {
      json = JSON.parse(responseText);
    } catch (parseErr) {
      const preview = responseText.slice(0, 160).replace(/\s+/g, ' ');
      const err = new Error(`Twenty non-JSON response: HTTP ${res.status} ${contentType} ${preview}`);
      err.retryable = res.status === 502 || res.status === 503 || res.status === 504 || !res.ok;
      if (err.retryable && attempt < retries) {
        await new Promise(r => setTimeout(r, attempt * 1000));
        continue;
      }
      throw err;
    }
    if (!res.ok) {
      const err = new Error(`Twenty HTTP ${res.status}: ${json?.errors?.[0]?.message || responseText.slice(0, 160)}`);
      err.retryable = res.status === 429 || res.status === 502 || res.status === 503 || res.status === 504;
      if (err.retryable && attempt < retries) {
        await new Promise(r => setTimeout(r, attempt * 1000));
        continue;
      }
      throw err;
    }
    if (json.errors) {
      const msg = json.errors[0].message;
      const isRateLimit = msg.includes('Limit reached') || msg.includes('rate limit');
      if (isRateLimit) {
        stats.rateLimitErrors++;
        stats.rateLimitPct = Math.round((stats.rateLimitErrors / Math.max(stats.accepted, 1)) * 100);
        if (attempt < retries) {
          const delay = attempt * 2000;
          console.warn(`  Twenty rate limited us despite pacing, waiting ${delay}ms (${attempt}/${retries})`);
          await new Promise(r => setTimeout(r, delay)); continue;
        }
        const err = new Error('Twenty API rate limit exhausted'); err.isRateLimit = true; throw err;
      }
      console.error('GraphQL error:', JSON.stringify(json.errors));
      throw new Error(msg);
    }
    return json.data;
  }
  throw new Error('gql: unexpected fallthrough');
}

// --- HELPERS ---
function normalizePhone(raw) {
  if (!raw) return null;
  let p = String(raw).replace(/\D/g, '');
  if (p.startsWith('91') && p.length > 10) p = p.slice(2);
  return p;
}

function parseBudget(raw) { if (!raw) return null; const num = parseInt(String(raw).replace(/[^0-9]/g, ''), 10); return isNaN(num) ? null : num; }
function parseBhk(raw) { if (!raw) return null; const match = String(raw).match(/(\d+)/); return match ? parseInt(match[1], 10) : null; }

function splitName(fullName) {
  if (!fullName) return { firstName: '', lastName: '' };
  const parts = fullName.trim().split(/\s+/);
  if (parts.length === 1) return { firstName: parts[0], lastName: '' };
  return { firstName: parts[0], lastName: parts.slice(1).join(' ') };
}

function sanitizeName(name) { if (!name) return 'Unknown'; return name.trim().replace(/[^a-zA-Z0-9]/g, ''); }

// Strip trailing role labels 99acres/Housing append to names, e.g. (Buyer), (Agent), (Broker), (Owner), (Seller)
// Handles both "Name (Buyer)" and "Name(Buyer)" (zero or more leading spaces before the paren).
function stripRoleLabel(name) {
  if (!name) return name;
  return name.replace(/\s*\([^)]*\)\s*$/, '').trim();
}

// --- CACHING ---
let buildingsCache = null;
let buildingsCacheTime = 0;
const BUILDINGS_CACHE_TTL = 5 * 60 * 1000;

async function getBuildings() {
  const now = Date.now();
  if (buildingsCache && (now - buildingsCacheTime) < BUILDINGS_CACHE_TTL) return buildingsCache;
  const data = await gql(`{ buildings(first: 1000) { edges { node { id name } } } }`);
  buildingsCache = (data?.buildings?.edges || []).map(e => e.node);
  buildingsCacheTime = Date.now();
  console.log(`  Loaded ${buildingsCache.length} buildings (cached for 5 min)`);
  return buildingsCache;
}

// --- PERSON OPS ---
async function findPersonByPhone(phone) {
  const data = await gql(`
    query FindPerson($phone: String) {
      people(filter: { phones: { primaryPhoneNumber: { eq: $phone } } }, first: 1) {
        edges { node { id name { firstName lastName } phones { primaryPhoneNumber } emails { primaryEmail } } }
      }
    }
  `, { phone });
  const edges = data?.people?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function findPersonByEmail(email) {
  if (!email) return null;
  const data = await gql(`
    query FindPersonByEmail($email: String) {
      people(filter: { emails: { primaryEmail: { eq: $email } } }, first: 1) {
        edges { node { id name { firstName lastName } phones { primaryPhoneNumber } emails { primaryEmail } } }
      }
    }
  `, { email });
  const edges = data?.people?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function findWorkspaceMemberByPhone(phone) {
  if (!phone) return null;
  const data = await gql(`
    query FindWorkspaceMemberByPhone($phone: String) {
      workspaceMembers(filter: { officePhone: { primaryPhoneNumber: { eq: $phone } } }, first: 1) {
        edges { node { id name { firstName lastName } userEmail officePhone { primaryPhoneNumber } } }
      }
    }
  `, { phone });
  const edges = data?.workspaceMembers?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function createPerson(payload, retries = 2) {
  const { firstName, lastName } = splitName(payload.name);
  const input = {
    name: { firstName, lastName },
    phones: { primaryPhoneNumber: payload.phoneDigits, primaryPhoneCountryCode: 'IN' },
    emails: payload.email ? { primaryEmail: payload.email } : undefined,
  };
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const data = await gql(`mutation CreatePerson($input: PersonCreateInput!) { createPerson(data: $input) { id name { firstName lastName } phones { primaryPhoneNumber } } }`, { input });
      if (data?.createPerson?.id) return data.createPerson;
      if (attempt < retries) { await new Promise(r => setTimeout(r, 500 * attempt)); continue; }
      return null;
    } catch (e) {
      if (attempt < retries) { await new Promise(r => setTimeout(r, 500 * attempt)); continue; }
      throw e;
    }
  }
  return null;
}

async function updatePerson(id, payload) {
  if (!payload.location) return;
  const input = {};
  // Person schema has 'city' (not 'address') and no 'budget' field
  if (payload.location) input.city = payload.location;
  try { await gql(`mutation UpdatePerson($id: ID!, $input: PersonUpdateInput!) { updatePerson(id: $id, data: $input) { id } }`, { id, input }); } catch (e) { /* non-fatal */ }
}

// --- BUYER OPS ---
async function findBuyerByPersonId(personId) {
  const data = await gql(`query FindBuyer($personId: UUID) { buyers(filter: { personId: { eq: $personId } }, first: 1) { edges { node { id name personId } } } }`, { personId });
  const edges = data?.buyers?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function createBuyer(payload, personId, retries = 2) {
  const name = sanitizeName(stripRoleLabel(payload.name)) || 'Unknown';
  // Buyer schema uses budgetMax (CurrencyCreateInput), not budget
  // NOTE: no " (Buyer)" suffix — inbound role labels are stripped and we don't append our own
  const input = { name, personId, source: payload.source || null, budgetMax: payload.budget ? { amountMicros: String(payload.budget * 1000000), currencyCode: 'INR' } : null };
  for (let attempt = 1; attempt <= retries; attempt++) {
    try {
      const data = await gql(`mutation CreateBuyer($input: BuyerCreateInput!) { createBuyer(data: $input) { id name } }`, { input });
      if (data?.createBuyer?.id) return data.createBuyer;
      if (attempt < retries) { await new Promise(r => setTimeout(r, 500 * attempt)); continue; }
      return null;
    } catch (e) {
      if (attempt < retries) { await new Promise(r => setTimeout(r, 500 * attempt)); continue; }
      throw e;
    }
  }
  return null;
}

async function updateBuyer(id, payload) {
  if (!payload.budget) return;
  // Buyer schema uses budgetMax (CurrencyUpdateInput), not budget
  try { await gql(`mutation UpdateBuyer($id: ID!, $input: BuyerUpdateInput!) { updateBuyer(id: $id, data: $input) { id } }`, { id, input: { budgetMax: { amountMicros: String(payload.budget * 1000000), currencyCode: 'INR' } } }); } catch (e) { /* non-fatal */ }
}

async function updateBuyerName(id, name) {
  const cleanName = stripRoleLabel(name || '').trim();
  if (!id || !cleanName) return;
  try {
    await gql(`mutation UpdateBuyerName($id: ID!, $input: BuyerUpdateInput!) { updateBuyer(id: $id, data: $input) { id name } }`, {
      id, input: { name: cleanName }
    });
  } catch (e) {
    console.error(`[Supabase/offer] Buyer name update failed: ${e.message}`);
    throw e;
  }
}

// --- BUILDING MATCH ---
async function findBuildingByName(projectName) {
  if (!projectName) return null;
  const buildings = await getBuildings();
  const needle = projectName.trim().toLowerCase();
  const exact = buildings.find(b => b.name.trim().toLowerCase() === needle);
  if (exact) return exact;
  const fuzzy = buildings.find(b => { const bName = b.name.trim().toLowerCase(); return needle.includes(bName) || bName.includes(needle); });
  if (fuzzy) return fuzzy;
  const needleWords = needle.split(/[\s\-_,.()]+/).filter(w => w.length > 2);
  if (needleWords.length > 0) {
    const wordMatch = buildings.find(b => {
      const bWords = b.name.trim().toLowerCase().split(/[\s\-_,.()]+/).filter(w => w.length > 2);
      if (bWords.length === 0) return false;
      const overlap = bWords.filter(w => needleWords.some(nw => nw.includes(w) || w.includes(nw)));
      return overlap.length >= Math.ceil(bWords.length * 0.5);
    });
    if (wordMatch) return wordMatch;
  }
  return null;
}

// --- PROPERTY OPS ---
async function findPropertyByBuildingId(buildingId) {
  if (!buildingId) return null;
  const data = await gql(`query FindPropertiesByBuilding($buildingId: UUID) { properties(filter: { buildingId: { eq: $buildingId } }, first: 5) { edges { node { id name buildingId } } } }`, { buildingId });
  const edges = data?.properties?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function createProperty(name, buildingId) {
  const data = await gql(`mutation CreateProperty($input: PropertyCreateInput!) { createProperty(data: $input) { id name } }`, { input: { name: name || 'Unknown Property', buildingId: buildingId || null } });
  return data.createProperty;
}

// --- ENQUIRY ---
async function createEnquiry(payload, buyerId, listingId, buildingId, propertyId, buyerName, buyerPhone, assignedAgentId) {
  const name = sanitizeName(buyerName) || 'Unknown';
  const phone = buyerPhone || '0000000000';
  const input = {
    enquiryNumber: `${name}_${phone}`,
    sourceDetail: payload.sourceDetail,
    enquiryType: 'BUY',
    statusDetail: 'NEW_LEAD',
    budget: payload.budget ? { amountMicros: String(payload.budget * 1000000), currencyCode: 'INR' } : null,
    buyerId,
    classifiedListingId: listingId || null,
    ...(buildingId ? { buildingId } : {}),
    ...(propertyId ? { propertyId } : {}),
    ...(assignedAgentId ? { assignedAgentId } : {}),
  };
  const data = await gql(`mutation CreateEnquiry($input: EnquiryCreateInput!) { createEnquiry(data: $input) { id enquiryNumber } }`, { input });
  if (!data?.createEnquiry?.id) throw new Error('ENQUIRY_CREATION_FAILED');
  return data.createEnquiry;
}

// --- CLASSIFIED LISTING ---
async function upsertClassifiedListing(payload, propertyId) {
  if (!payload.listingId) return null;
  const data = await gql(`query FindListing($id: String) { classifiedListings(filter: { listingId: { eq: $id } }, first: 1) { edges { node { id propertyId } } } }`, { id: payload.listingId });
  const edges = data?.classifiedListings?.edges || [];
  if (edges.length) return { id: edges[0].node.id, action: 'found' };
  // Only include non-null fields — GraphQL rejects null/extra fields on ClassifiedListing
  const input = { listingId: payload.listingId };
  if (payload.listingUrl) input.listingUrl = payload.listingUrl;
  if (propertyId) input.propertyId = propertyId;
  const createData = await gql(`mutation CreateListing($input: ClassifiedListingCreateInput!) { createClassifiedListing(data: $input) { id } }`, { input });
  return { id: createData.createClassifiedListing.id, action: 'created' };
}

// --- NORMALIZERS ---
function normalize99Acres(body) {
  // 99acres uses multiple field names across different bundle types
  // listingId (camelCase), listingID (ID caps), listing_id (snake), ListingID (Pascal+ID), id, propertyId
  const listingId = body.listingId || body.listingID || body.ListingID || body.listing_id || body.ListingId || body.id || body.propertyId || null;
  const listingUrl = body.listingUrl || body.listingURL || body.listing_url || null;
  console.log(`  [99acres] Raw payload keys: ${Object.keys(body).join(', ')}`);
  return {
    name: body.name || body.Name || null, phoneDigits: normalizePhone(body.mobile || body.Mobile || body.phone),
    email: body.email || body.Email || null, budget: parseBudget(body.budget || body.Budget),
    bhk: parseBhk(body.bhk || body.BHK || body.bhkType), location: body.project || body.Project || body.location || null,
    landmark: body.landmark || null, listingId, listingUrl,
    platform: 'NINETYNINE_ACRES', sourceDetail: 'NINETYNINE_ACRES', notes: `MasterProjectID: ${body.MasterProjectID || ''}`, raw: body,
  };
}

function normalizeHousing(body) {
  return {
    name: body.name || null, phoneDigits: normalizePhone(body.mobile), email: body.email || null,
    budget: parseBudget(body.budget), bhk: parseBhk(body.bhkType), location: body.project || null,
    landmark: body.location || null, listingId: body.housing_listingID || null, listingUrl: null,
    platform: 'HOUSING', sourceDetail: 'HOUSING', notes: `City: ${body.city || ''}, Subsource: ${body.subsource || ''}`, raw: body,
  };
}

function normalizeJumbo(body) {
  return {
    name: body.name || null, phoneDigits: normalizePhone(body.phone || body.mobile), email: body.email || null,
    budget: parseBudget(body.budget), bhk: parseBhk(body.bhk || body.bhkType), location: body.location || body.project || null,
    landmark: body.landmark || null, listingId: body.listingId || null, listingUrl: body.listingUrl || null,
    platform: null, sourceDetail: 'WEBSITE', notes: body.message || body.notes || null, raw: body,
  };
}

// --- MAIN PIPELINE ---
async function processPortalEnquiry(source, payload) {
  // Guard: reject enquiries without phone numbers to prevent creating un-matchable persons (JUM-661)
  if (!payload.phoneDigits) {
    console.error(`  [${source}] Missing phone number — skipping enquiry to prevent orphan person`);
    throw new Error('MISSING_PHONE');
  }
  const dedupKey = payload._dedupKey || null;
  // Strip inbound role labels (e.g. "(Buyer)", "(Agent)") from the name so they don't pollute Person/Buyer records
  payload.name = stripRoleLabel(payload.name);
  let person = await findPersonByPhone(payload.phoneDigits);
  if (person) { console.log(`  Found person: ${person.id}`); await updatePerson(person.id, payload); }
  else { console.log(`  Creating new person`); person = await createPerson(payload); }
  if (!person) { console.error(`  [${source}] Person creation returned null — skipping enquiry`); throw new Error('PERSON_CREATION_FAILED'); }

  let buyer = await findBuyerByPersonId(person.id);
  if (buyer) { console.log(`  Found buyer: ${buyer.id}`); await updateBuyer(buyer.id, payload); }
  else {
    console.log(`  Creating new buyer for person ${person.id}`); buyer = await createBuyer(payload, person.id);
    if (!buyer) { console.error(`  [${source}] Buyer creation returned null after retries — skipping enquiry for person ${person.id}`); throw new Error('BUYER_CREATION_FAILED'); }
    console.log(`  Created buyer: ${buyer.id}`);
  }

  let building = null;
  if (payload.location) {
    building = await findBuildingByName(payload.location);
    if (building) console.log(`  Matched building: ${building.name} (${building.id})`);
    else console.log(`  No building match for: ${payload.location}`);
  }

  let listing = null;
  let property = null;

  if (building?.id) {
    property = await findPropertyByBuildingId(building.id);
    if (property) console.log(`  Found existing property in building: ${property.name} (${property.id})`);
    else { const propName = payload.location || building.name || 'Unknown Property'; property = await createProperty(propName, building.id); console.log(`  Created new property: ${property.name} (${property.id})`); }
  }

  if (payload.listingId) { listing = await upsertClassifiedListing(payload, property?.id); console.log(`  Listing ${listing.action}: ${listing.id}`); }

  // If listing has a property, link it and use that property for the enquiry
  if (listing?.id) {
    const listingData = await gql(`query GetListing($id: UUID) { classifiedListings(filter: { id: { eq: $id } }, first: 1) { edges { node { id propertyId } } } }`, { id: listing.id });
    const listingNode = listingData?.classifiedListings?.edges?.[0]?.node;
    if (listingNode?.propertyId) {
      // Listing has a property — link it to the enquiry
      if (!listingNode.propertyId) {
        await gql(`mutation UpdateListingProperty($id: ID!, $input: ClassifiedListingUpdateInput!) { updateClassifiedListing(id: $id, data: $input) { id } }`, { id: listing.id, input: { propertyId: property.id } });
      }
      // Use the listing's property if available, fall back to building property
      property = { id: listingNode.propertyId };
    }
  }

  // Create enquiry assigned to Aashish (will be reassigned to zone agent after 5 min)
  const enquiry = await createEnquiry(
    payload, buyer.id, listing?.id, building?.id, property?.id,
    buyer.name, payload.phoneDigits, AASHISH_WORKSPACE_MEMBER_ID
  );
  console.log(`  Created enquiry: ${enquiry.id} (assigned to Aashish)`);
  await commitPortalEnquiry(dedupKey, enquiry.id);

  // If building has a zone, queue reassignment to zone agent after 5 minutes
  if (building?.id) {
    const zoneAgent = await findZoneAgentForBuilding(building.id);
    if (zoneAgent) {
      REASSIGNMENT_QUEUE.push({
        enquiryId: enquiry.id,
        buildingId: building.id,
        agentId: zoneAgent.agentId,
        agentName: zoneAgent.agentName,
        reassignAt: Date.now() + REASSIGNMENT_DELAY_MS,
      });
      console.log(`  [Reassign] Queued enquiry ${enquiry.id} for reassignment to ${zoneAgent.agentName} in 5 min`);
    } else {
      console.log(`  [Reassign] No zone agent for building ${building.name} — enquiry stays with Aashish`);
    }
  }

  return {
    success: true,
    person: { id: person.id, name: person.name },
    buyer: { id: buyer.id, name: buyer.name },
    enquiry: { id: enquiry.id, number: enquiry.enquiryNumber },
    listing,
    building: building ? { id: building.id, name: building.name } : null,
    property: property ? { id: property.id, name: property.name } : null,
  };
}

// ============================================
// SUPABASE WEBHOOK HANDLERS
// ============================================

// Supabase webhook payload: { type: "INSERT"|"UPDATE"|"DELETE", table: "...", record: {...}, old_record: {...} }

async function handleWebSignup(record) {
  // record: { id, name, phone_number, email, drop_reason, created_at, updated_at, internal_id, is_active, disqualification_reason }
  const supabaseId = record.id;
  console.log(`[Supabase/web_signup] Processing: ${record.name} (${record.phone_number})`);

  // Idempotent: already synced
  if (record.internal_id) {
    console.log(`[Supabase/web_signup] Already synced: internal_id=${record.internal_id}`);
    return { success: true, skipped: true, reason: 'already synced' };
  }

  const cleanPhone = normalizePhone(record.phone_number);
  if (!cleanPhone) {
    console.log(`[Supabase/web_signup] Invalid phone: ${record.phone_number}`);
    return { success: false, error: 'invalid phone' };
  }

  // Find or create person
  let person = await findPersonByPhone(cleanPhone);
  if (person) {
    console.log(`[Supabase/web_signup] Found existing person: ${person.id}`);
  } else {
    const { firstName, lastName } = splitName(record.name);
    const input = {
      name: { firstName, lastName },
      phones: { primaryPhoneNumber: cleanPhone, primaryPhoneCountryCode: 'IN' },
      emails: record.email ? { primaryEmail: record.email } : undefined,
    };
    const data = await gql(`mutation CreatePerson($input: PersonCreateInput!) { createPerson(data: $input) { id name { firstName lastName } } }`, { input });
    person = data.createPerson;
    console.log(`[Supabase/web_signup] Created person: ${person.id}`);
  }

  // Find or create buyer
  let buyer = await findBuyerByPersonId(person.id);
  if (buyer) {
    console.log(`[Supabase/web_signup] Found existing buyer: ${buyer.id}`);
  } else {
    const name = record.name || 'Unknown';
    const input = { name, personId: person.id };
    try {
      const data = await gql(`mutation CreateBuyer($input: BuyerCreateInput!) { createBuyer(data: $input) { id name } }`, { input });
      buyer = data.createBuyer;
      console.log(`[Supabase/web_signup] Created buyer: ${buyer.id}`);
    } catch (e) {
      // If buyer creation fails (e.g. duplicate), try to find existing
      if (e.message?.includes('duplicate') || e.message?.includes('unique')) {
        buyer = await findBuyerByPersonId(person.id);
      }
      if (!buyer) throw e;
    }
  }

  // Create enquiry assigned to Aashish
  const enquiryName = `Web Signup: ${record.name || 'Unknown'}`;
  const enquiryInput = {
    name: enquiryName,
    enquiryNumber: `${record.name || 'Unknown'}_${cleanPhone}`,
    enquiryType: 'BUY',
    statusDetail: 'NEW_LEAD',
    sourceDetail: 'WEBSITE',
    buyerId: buyer.id,
    assignedAgentId: AASHISH_WORKSPACE_MEMBER_ID,
  };
  try {
    const enquiryData = await gql(`mutation CreateEnquiry($input: EnquiryCreateInput!) { createEnquiry(data: $input) { id enquiryNumber } }`, { input: enquiryInput });
    console.log(`[Supabase/web_signup] Created enquiry: ${enquiryData.createEnquiry.id}`);
    // Queue reassignment to Ridhima after 5 minutes
    REASSIGNMENT_QUEUE.push({
      enquiryId: enquiryData.createEnquiry.id,
      targetAgentId: RIDHIMA_WORKSPACE_MEMBER_ID,
      targetAgentName: 'Ridhima',
      reassignAt: Date.now() + REASSIGNMENT_DELAY_MS,
    });
    console.log(`  [Reassign] Queued enquiry ${enquiryData.createEnquiry.id} for reassignment to Ridhima in 5 min`);
  } catch (e) {
    // Non-fatal: person+buyer exist, enquiry might be duplicate
    console.log(`[Supabase/web_signup] Enquiry creation issue: ${e.message}`);
  }

  // Write back internal_id to Supabase
  try {
    await supabasePatch('external_user', supabaseId, { internal_id: buyer.id });
    console.log(`[Supabase/web_signup] Wrote back internal_id: ${buyer.id}`);
  } catch (e) {
    console.log(`[Supabase/web_signup] Write-back failed: ${e.message}`);
  }

  return { success: true, personId: person.id, buyerId: buyer.id };
}

async function handleSellerSignup(record) {
  // record: { id, name, phone_number, building_name, additional_details, created_at, updated_at }
  const supabaseId = record.id;
  console.log(`[Supabase/seller_signup] Processing: ${record.name} (${record.phone_number})`);

  if (record.internal_id) {
    console.log(`[Supabase/seller_signup] Already synced: internal_id=${record.internal_id}`);
    return { success: true, skipped: true, reason: 'already synced' };
  }

  const cleanPhone = normalizePhone(record.phone_number);
  if (!cleanPhone) {
    console.log(`[Supabase/seller_signup] Invalid phone: ${record.phone_number}`);
    return { success: false, error: 'invalid phone' };
  }

  // Find or create person
  let person = await findPersonByPhone(cleanPhone);
  if (person) {
    console.log(`[Supabase/seller_signup] Found existing person: ${person.id}`);
  } else {
    const { firstName, lastName } = splitName(record.name);
    const input = {
      name: { firstName, lastName },
      phones: { primaryPhoneNumber: cleanPhone, primaryPhoneCountryCode: 'IN' },
    };
    const data = await gql(`mutation CreatePerson($input: PersonCreateInput!) { createPerson(data: $input) { id name { firstName lastName } } }`, { input });
    person = data.createPerson;
    console.log(`[Supabase/seller_signup] Created person: ${person.id}`);
  }

  // Create seller in CRM
  let seller = null;
  const sellerInput = {
    name: record.name || 'Unknown',
    personId: person.id,
    stage: 'NEW_ENQUIRY',
    source: 'WEBSITE',
  };
  try {
    const data = await gql(`mutation CreateSeller($input: SellerCreateInput!) { createSeller(data: $input) { id name } }`, { input: sellerInput });
    seller = data.createSeller;
    console.log(`[Supabase/seller_signup] Created seller: ${seller.id}`);
  } catch (e) {
    console.log(`[Supabase/seller_signup] Seller creation issue: ${e.message}`);
  }

  // Create Related Note from seller form fields and link to seller
  if (seller?.id && (record.building_name || record.additional_details)) {
    try {
      const bodyParts = [];
      if (record.building_name) bodyParts.push(`**Building Name:** ${record.building_name}`);
      if (record.additional_details) bodyParts.push(`**Additional Details:**\n${record.additional_details}`);
      const markdown = bodyParts.join('\n\n');
      const noteData = await gql(`mutation CreateNote($input: NoteCreateInput!) { createNote(data: $input) { id } }`, {
        input: { title: `Seller Signup Note`, bodyV2: { markdown } }
      });
      const noteId = noteData.createNote.id;
      await gql(`mutation CreateNoteTarget($input: NoteTargetCreateInput!) { createNoteTarget(data: $input) { id } }`, {
        input: { noteId, targetSellerId: seller.id }
      });
      console.log(`[Supabase/seller_signup] Created note ${noteId} linked to seller ${seller.id}`);
    } catch (e) {
      console.log(`[Supabase/seller_signup] Note creation failed: ${e.message}`);
    }
  }

  // Write back internal_id to Supabase
  const crmId = seller?.id || person.id;
  try {
    await supabasePatch('seller', supabaseId, { internal_id: crmId });
    console.log(`[Supabase/seller_signup] Wrote back internal_id: ${crmId}`);
  } catch (e) {
    console.log(`[Supabase/seller_signup] Write-back failed: ${e.message}`);
  }

  return { success: true, personId: person.id, sellerId: seller?.id };
}

async function handleVisit(record) {
  // record: { id, external_user_id, user_id, listing_id, scheduled_at, status, confirmed_at, ... }
  const supabaseId = record.id;
  console.log(`[Supabase/visit] Processing visit: ${supabaseId}`);

  if (record.internal_id) {
    return { success: true, skipped: true, reason: 'already synced' };
  }

  // Resolve buyer: fetch phone from Supabase, then find/create in CRM
  // user_id → authenticated website users (visitSource: WEBSITE)
  // external_user_id → non-authenticated users from other channels (visitSource: ANANYA)
  let crmBuyerId = null;
  let buyerPhone = null;
  let buyerName = null;
  let visitSource = 'WEBSITE';
  const userId = record.user_id;
  const extUserId = record.external_user_id;
  if (userId) {
    const supaUser = await fetchSupabaseUser(userId);
    if (supaUser?.phone_number) {
      buyerPhone = normalizePhone(supaUser.phone_number);
      buyerName = supaUser?.name || null;
      visitSource = 'WEBSITE';
      console.log(`[Supabase/visit] Found phone ${buyerPhone} for user ${userId} (WEBSITE), name=${buyerName}`);
    }
  } else if (extUserId) {
    const supaExtUser = await fetchSupabaseExternalUser(extUserId);
    if (supaExtUser?.phone_number) {
      buyerPhone = normalizePhone(supaExtUser.phone_number);
      buyerName = supaExtUser?.name || null;
      visitSource = 'ANANYA';
      console.log(`[Supabase/visit] Found phone ${buyerPhone} for external_user ${extUserId} (ANANYA), name=${buyerName}`);
    }
  }
  if (buyerPhone) {
    let person = await findPersonByPhone(buyerPhone);
    if (person) {
      crmBuyerId = (await findBuyerByPersonId(person.id))?.id;
    }
    if (!crmBuyerId) {
      // JUM-661: Only create person if not found; always create buyer on existing person
      if (!person) {
        const { firstName, lastName } = splitName(buyerName || 'Unknown');
        const personInput = {
          name: { firstName, lastName },
          phones: { primaryPhoneNumber: buyerPhone, primaryPhoneCountryCode: 'IN' },
        };
        try {
          const pData = await gql(`mutation CreatePerson($input: PersonCreateInput!) { createPerson(data: $input) { id } }`, { input: personInput });
          person = { id: pData.createPerson.id };
          console.log(`[Supabase/visit] Created person: ${person.id}`);
        } catch (e) {
          console.log(`[Supabase/visit] Failed to create person: ${e.message}`);
        }
      }
      if (person) {
        try {
          const bData = await gql(`mutation CreateBuyer($input: BuyerCreateInput!) { createBuyer(data: $input) { id } }`, { input: { name: buyerName ? stripRoleLabel(buyerName) : 'Unknown (Buyer)', personId: person.id } });
          crmBuyerId = bData.createBuyer.id;
          console.log(`[Supabase/visit] Created buyer ${crmBuyerId} for person ${person.id}`);
        } catch (e) {
          console.log(`[Supabase/visit] Failed to create buyer for person ${person.id}: ${e.message}`);
        }
      }
    }
  }
  if (!crmBuyerId) {
    console.log(`[Supabase/visit] No buyer mapping found for user_id=${record.user_id}, external_user_id=${record.external_user_id}`);
    return { success: false, error: 'buyer not synced yet' };
  }

  // Get person ID from buyer for pointOfContactId
  let crmPersonId = null;
  if (crmBuyerId) {
    try {
      const buyerData = await gql('query GetBuyerPerson($id: UUID) { buyers(filter: { id: { eq: $id } }, first: 1) { edges { node { personId } } } }', { id: crmBuyerId });
      crmPersonId = buyerData?.buyers?.edges?.[0]?.node?.personId || null;
    } catch (e) {
      console.log(`[Supabase/offer] Failed to get person from buyer: ${e.message}`);
    }
  }

  // Resolve listing → CRM property via glide_serial_number → serialNumber
  let crmPropertyId = null;
  if (record.listing_id) {
    // First try internal_id mapping
    crmPropertyId = await lookupInternalId('listing', record.listing_id);
    // If no internal_id, try fetching the listing from Supabase to get glide_serial_number
    if (!crmPropertyId && SUPABASE_URL && SUPABASE_SERVICE_KEY) {
      try {
        const listingUrl = `${SUPABASE_URL}/rest/v1/listing?id=eq.${record.listing_id}&select=glide_serial_number`;
        const listingRes = await fetch(listingUrl, {
          headers: { 'apikey': SUPABASE_SERVICE_KEY, 'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}` },
        });
        if (listingRes.ok) {
          const listingData = await listingRes.json();
          const glideSerial = listingData?.[0]?.glide_serial_number;
          if (glideSerial) {
            const propData = await gql(`query FindPropertyBySerial($serial: Float) { properties(filter: { serialNumber: { eq: $serial } }, first: 1) { edges { node { id } } } }`, { serial: glideSerial });
            crmPropertyId = propData?.properties?.edges?.[0]?.node?.id || null;
            if (crmPropertyId) console.log(`[Supabase/visit] Matched property via serialNumber ${glideSerial}: ${crmPropertyId}`);
          }
        }
      } catch (e) {
        console.log(`[Supabase/visit] Listing lookup failed: ${e.message}`);
      }
    }
  }

  // Fetch buyer name and property name for the visit title
  let propertyName = 'Unknown';
  try {
    const buyerData = await gql(`query GetBuyerName($id: UUID) { buyers(filter: { id: { eq: $id } }, first: 1) { edges { node { id name } } } }`, { id: crmBuyerId });
    buyerName = buyerData?.buyers?.edges?.[0]?.node?.name || 'Unknown';
  } catch(e) {}
  if (crmPropertyId) {
    try {
      const propData = await gql(`query GetPropertyName($id: UUID) { properties(filter: { id: { eq: $id } }, first: 1) { edges { node { id name } } } }`, { id: crmPropertyId });
      propertyName = propData?.properties?.edges?.[0]?.node?.name || 'Unknown';
    } catch(e) {}
  }

  const visitInput = {
    name: `${buyerName} - ${propertyName}`,
    buyerProfileId: crmBuyerId,
    visitSource,
    ...(crmPropertyId ? { propertyId: crmPropertyId } : {}),
    scheduledAt: record.scheduled_at ? record.scheduled_at + '+05:30' : null,
    ...(record.confirmed_at ? { confirmedAt: record.confirmed_at + '+05:30' } : {}),
  };

  // Check for existing visit (buyer + time + property — same buyer can visit different properties)
  const existingData = await gql(`
    query FindVisit($buyerId: UUID, $scheduledAt: DateTime, $propertyId: UUID) {
      visits(filter: { buyerProfileId: { eq: $buyerId }, scheduledAt: { eq: $scheduledAt }, propertyId: { eq: $propertyId } }, first: 1) {
        edges { node { id } }
      }
    }
  `, { buyerId: crmBuyerId, scheduledAt: record.scheduled_at ? record.scheduled_at + '+05:30' : null, propertyId: crmPropertyId });
  const existing = existingData?.visits?.edges?.[0]?.node;

  let crmVisitId = existing?.id || null;
  if (existing) {
    await gql(`mutation UpdateVisit($id: ID!, $input: VisitUpdateInput!) { updateVisit(id: $id, data: $input) { id } }`,
      { id: existing.id, input: visitInput });
    console.log(`[Supabase/visit] Updated existing visit: ${existing.id}`);
  } else {
    const data = await gql(`mutation CreateVisit($input: VisitCreateInput!) { createVisit(data: $input) { id } }`, { input: visitInput });
    crmVisitId = data.createVisit.id;
    console.log(`[Supabase/visit] Created visit: ${crmVisitId}`);
  }

  // Write back the CRM VISIT id (not the buyer id) to Supabase so internal_id stays
  // unique per visit. Retry once on transient failure; throw if it still fails so the
  // webhook is marked Failed instead of silently losing the sync marker.
  if (crmVisitId) {
    let lastErr = null;
    for (let attempt = 1; attempt <= 2; attempt++) {
      try {
        await supabasePatch('visit', supabaseId, { internal_id: crmVisitId });
        console.log(`[Supabase/visit] Wrote back internal_id (CRM visit): ${crmVisitId}`);
        lastErr = null;
        break;
      } catch (e) {
        lastErr = e;
        console.error(`[Supabase/visit] Write-back attempt ${attempt} failed: ${e.message}`);
        if (attempt < 2) await new Promise(r => setTimeout(r, 1000));
      }
    }
    if (lastErr) throw lastErr;
  }

  await markVisitRetryDone(supabaseId);
  return { success: true };
}

async function processVisitRetryQueue() {
  try {
    const rows = await claimVisitRetries();
    for (const row of rows) {
      try {
        await handleVisit(row.payload);
        await markVisitRetryDone(row.source_id);
        console.log(`[Supabase/visit-retry] Recovered ${row.source_id}`);
      } catch (err) {
        await saveVisitRetry(row.payload, err).catch(e => console.error(`[Supabase/visit-retry] Save failed ${row.source_id}:`, e.message));
        console.error(`[Supabase/visit-retry] Failed ${row.source_id}:`, err.message);
      }
    }
  } catch (err) {
    console.error('[Supabase/visit-retry] Poll failed:', err.message);
  }
}

async function calculateOfferCategory(buyerProfileId, propertyId, offerCreatedAt) {
  if (!buyerProfileId || !propertyId || !offerCreatedAt) return 'BEFORE_VISIT';
  const visitData = await gql(`
    query FindCompletedVisitBeforeOffer($buyerId: UUID, $propertyId: UUID, $offerCreatedAt: DateTime) {
      visits(filter: {
        buyerProfileId: { eq: $buyerId }
        propertyId: { eq: $propertyId }
        status: { eq: COMPLETED }
        scheduledAt: { lt: $offerCreatedAt }
      }, first: 1) { edges { node { id } } }
    }
  `, { buyerId: buyerProfileId, propertyId, offerCreatedAt });
  return (visitData?.visits?.edges?.length || 0) > 0 ? 'AFTER_VISIT' : 'BEFORE_VISIT';
}

async function handleOffer(record) {
  // record: { id, external_user_id, user_id, listing_id, offer_price, note, status, offer_price_submitted_at, ... }
  const supabaseId = record.id;
  console.log(`[Supabase/offer] Processing offer: ${supabaseId}, price=${record.offer_price}`);

  if (record.internal_id) {
    return { success: true, skipped: true, reason: 'already synced' };
  }

  // Resolve the actual buyer from Supabase identity fields.
  // A submitter may use a workspace member's phone on behalf of the buyer;
  // never attach an offer to a workspace member just because their phone matches.
  let crmBuyerId = null;
  let buyerPhone = null;
  let buyerEmail = null;
  let buyerName = null;
  const userId = record.user_id;
  const extUserId = record.external_user_id;
  const supaUser = userId ? await fetchSupabaseUser(userId) : null;
  const supaExtUser = !userId && extUserId ? await fetchSupabaseExternalUser(extUserId) : null;
  const sourceUser = supaUser || supaExtUser;
  if (sourceUser) {
    buyerPhone = normalizePhone(sourceUser.phone_number);
    buyerEmail = sourceUser.email || null;
    buyerName = sourceUser.name || null;
    console.log(`[Supabase/offer] Source buyer: name=${buyerName}, email=${buyerEmail || 'none'}, phone=${buyerPhone || 'none'}`);
  }

  // Prefer the buyer's email/name identity. Only use the submitted phone when it
  // does not belong to a workspace member (agent/submitter).
  let person = buyerEmail ? await findPersonByEmail(buyerEmail) : null;
  const submitterMember = buyerPhone ? await findWorkspaceMemberByPhone(buyerPhone) : null;
  const phoneBelongsToWorkspaceMember = !!submitterMember;
  if (!person && buyerPhone && !phoneBelongsToWorkspaceMember) person = await findPersonByPhone(buyerPhone);
  if (person) {
    const existingBuyer = await findBuyerByPersonId(person.id);
    crmBuyerId = existingBuyer?.id || null;
    if (crmBuyerId && buyerName) await updateBuyerName(crmBuyerId, buyerName);
  }

  if (!crmBuyerId) {
    if (!person) {
      const { firstName, lastName } = splitName(buyerName || 'Unknown');
      const personInput = {
        name: { firstName, lastName },
        ...(phoneBelongsToWorkspaceMember || !buyerPhone ? {} : { phones: { primaryPhoneNumber: buyerPhone, primaryPhoneCountryCode: 'IN' } }),
        ...(buyerEmail ? { emails: { primaryEmail: buyerEmail } } : {}),
      };
      const pData = await gql(`mutation CreatePerson($input: PersonCreateInput!) { createPerson(data: $input) { id } }`, { input: personInput });
      person = { id: pData.createPerson.id };
      console.log(`[Supabase/offer] Created buyer person ${person.id} using source identity`);
    }
    const bData = await gql(`mutation CreateBuyer($input: BuyerCreateInput!) { createBuyer(data: $input) { id } }`, {
      input: { name: buyerName ? stripRoleLabel(buyerName) : 'Unknown', personId: person.id }
    });
    crmBuyerId = bData.createBuyer.id;
    console.log(`[Supabase/offer] Created buyer ${crmBuyerId} for person ${person.id}; submitterPhone=${phoneBelongsToWorkspaceMember}`);
  }
  if (!crmBuyerId) {
    console.log(`[Supabase/offer] No buyer mapping found for user=${userId}`);
    return { success: false, error: 'buyer not synced yet' };
  }

  // Get person ID from buyer for pointOfContactId
  let crmPersonId = null;
  if (crmBuyerId) {
    try {
      const buyerData = await gql('query GetBuyerPerson($id: UUID) { buyers(filter: { id: { eq: $id } }, first: 1) { edges { node { personId } } } }', { id: crmBuyerId });
      crmPersonId = buyerData?.buyers?.edges?.[0]?.node?.personId || null;
    } catch (e) {
      console.log(`[Supabase/offer] Failed to get person from buyer: ${e.message}`);
    }
  }

  // Resolve listing → CRM property via glide_serial_number → serialNumber
  let crmPropertyId = null;
  let propertySerial = null;
  let propertyConfig = null;
  if (record.listing_id) {
    crmPropertyId = await lookupInternalId('listing', record.listing_id);
    if (!crmPropertyId && SUPABASE_URL && SUPABASE_SERVICE_KEY) {
      try {
        const listingUrl = `${SUPABASE_URL}/rest/v1/listing?id=eq.${record.listing_id}&select=glide_serial_number`;
        const listingRes = await fetch(listingUrl, {
          headers: { 'apikey': SUPABASE_SERVICE_KEY, 'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}` },
        });
        if (listingRes.ok) {
          const listingData = await listingRes.json();
          const glideSerial = listingData?.[0]?.glide_serial_number;
          if (glideSerial) {
            const propData = await gql(`query FindPropertyBySerial($serial: Float) { properties(filter: { serialNumber: { eq: $serial } }, first: 1) { edges { node { id serialNumber configuration buildingId } } } }`, { serial: glideSerial });
            const propNode = propData?.properties?.edges?.[0]?.node;
            crmPropertyId = propNode?.id || null;
            propertySerial = propNode?.serialNumber || null;
            propertyConfig = propNode?.configuration || null;
            if (crmPropertyId) console.log(`[Supabase/offer] Matched property via serialNumber ${glideSerial}: ${crmPropertyId}`);
          }
        }
      } catch (e) {
        console.log(`[Supabase/offer] Listing lookup failed: ${e.message}`);
      }
    }
  }

  // Resolve building from property for title format and zone lead lookup
  let buildingName = null;
  let crmBuildingId = null;
  if (crmPropertyId) {
    try {
      const propData = await gql(`query GetPropertyBuilding($id: UUID) { properties(filter: { id: { eq: $id } }, first: 1) { edges { node { buildingId } } } }`, { id: crmPropertyId });
      crmBuildingId = propData?.properties?.edges?.[0]?.node?.buildingId || null;
      if (crmBuildingId) {
        const bldgData = await gql(`query GetBuildingName($id: UUID) { buildings(filter: { id: { eq: $id } }, first: 1) { edges { node { id name } } } }`, { id: crmBuildingId });
        buildingName = bldgData?.buildings?.edges?.[0]?.node?.name || null;
        console.log(`[Supabase/offer] Property ${crmPropertyId} → building: ${buildingName} (${crmBuildingId})`);
      }
    } catch (e) {
      console.log(`[Supabase/offer] Building lookup failed: ${e.message}`);
    }
    // Fallback: if we got serialNumber from the listing query but not from property query
    if (!propertySerial) {
      try {
        const propData = await gql(`query GetPropertySerial($id: UUID) { properties(filter: { id: { eq: $id } }, first: 1) { edges { node { serialNumber configuration } } } }`, { id: crmPropertyId });
        propertySerial = propData?.properties?.edges?.[0]?.node?.serialNumber || null;
        propertyConfig = propData?.properties?.edges?.[0]?.node?.configuration || propertyConfig;
      } catch (e) { /* non-fatal */ }
    }
  }

  // Bug fix #1: offer_price is in LAKHS (e.g. 95 = 95 lakhs = ₹95,00,000).
  // Strip commas if string, parse as float, convert lakhs→rupees (×100000), then rupees→micros (×1000000).
  const rawPrice = typeof record.offer_price === 'string' ? record.offer_price.replace(/,/g, '') : record.offer_price;
  const priceInLakhs = rawPrice ? parseFloat(rawPrice) : 0;
  const priceAmount = priceInLakhs ? Math.round(priceInLakhs * 100000 * 1000000) : 0;

  // Bug fix #3: title format J-{Serial}-{Building}-{Config}
  const serialPart = propertySerial || 'Unknown';
  const buildingPart = buildingName || 'Unknown';
  const configPart = propertyConfig || 'Unknown';
  const offerTitle = `J-${serialPart}-${buildingPart}-${configPart}`;

  // Bug fix #4: look up zone lead for the building and assign as owner (closing agent)
  let ownerId = null;
  if (crmBuildingId) {
    const zoneAgent = await findZoneAgentForBuilding(crmBuildingId);
    if (zoneAgent) {
      ownerId = zoneAgent.agentId;
      console.log(`[Supabase/offer] Zone lead for building ${buildingName}: ${zoneAgent.agentName} (${ownerId})`);
    } else {
      console.log(`[Supabase/offer] No zone lead found for building ${buildingName} (${crmBuildingId})`);
    }
  }

  const opportunityInput = {
    name: offerTitle,
    amount: { amountMicros: String(priceAmount), currencyCode: 'INR' },
    stage: 'NEW',
    offerSource: 'WEBSITE_OFFER',
    ...(crmPropertyId ? { propertyNewId: crmPropertyId } : {}),
    ...(crmPersonId ? { pointOfContactId: crmPersonId } : {}),
    // Bug fix #4: assign zone lead as owner (closing agent)
    ...(ownerId ? { ownerId } : {}),
  };

  const data = await gql(`mutation CreateOpportunity($input: OpportunityCreateInput!) { createOpportunity(data: $input) { id name createdAt } }`, { input: opportunityInput });
  const opportunityId = data.createOpportunity.id;
  const opportunityCreatedAt = data.createOpportunity.createdAt;
  const offerCategory = await calculateOfferCategory(crmBuyerId, crmPropertyId, opportunityCreatedAt);
  await gql(`mutation SetOfferCategory($id: ID!, $input: OpportunityUpdateInput!) { updateOpportunity(id: $id, data: $input) { id offerCategory } }`, {
    id: opportunityId,
    input: { offerCategory },
  });
  console.log(`[Supabase/offer] Created opportunity: ${opportunityId} "${offerTitle}" amount=${priceAmount} micros, owner=${ownerId || 'none'}, category=${offerCategory}`);

  // Supabase uses offer_note/counter_offer_note/final_note. Create each as a
  // standard Twenty Note and attach it via noteTarget to this opportunity.
  // IMPORTANT: preserve the source note verbatim. Do not substitute a generated
  // buyer/phone/price summary: the free-text note contains the real intent signal.
  const offerNotes = [
    ['Offer Note', record.offer_note],
    ['Counter Offer Note', record.counter_offer_note],
    ['Final Offer Note', record.final_note],
  ].filter(([, text]) => text !== null && text !== undefined && String(text).trim());
  for (const [title, text] of offerNotes) {
    try {
      const noteData = await gql(`mutation CreateNote($input: NoteCreateInput!) { createNote(data: $input) { id } }`, {
        input: { title, bodyV2: { markdown: String(text) } }
      });
      const noteId = noteData.createNote.id;
      await gql(`mutation CreateNoteTarget($input: NoteTargetCreateInput!) { createNoteTarget(data: $input) { id } }`, {
        input: { noteId, targetOpportunityId: opportunityId }
      });
      console.log(`[Supabase/offer] Created ${title} ${noteId} linked to opportunity ${opportunityId}`);
    } catch (e) {
      console.error(`[Supabase/offer] ${title} creation failed: ${e.message}`);
      throw e;
    }
  }

  // Write back
  try {
    await supabasePatch('offer', supabaseId, { internal_id: opportunityId });
  } catch (e) {
    console.log(`[Supabase/offer] Write-back failed: ${e.message}`);
  }

  return { success: true, opportunityId };
}

// --- SUPABASE HELPERS ---

// Lookup CRM UUID from Supabase table's internal_id column via CRM GraphQL
const internalIdCache = new Map();
const INTERNAL_ID_CACHE_TTL = 5 * 60 * 1000; // 5 min

async function lookupInternalId(table, supabaseUuid) {
  if (!supabaseUuid) return null;
  const cacheKey = `${table}:${supabaseUuid}`;
  const cached = internalIdCache.get(cacheKey);
  if (cached && (Date.now() - cached.ts) < INTERNAL_ID_CACHE_TTL) {
    return cached.internalId;
  }
  
  // For external_user: internal_id = buyer.id, so query buyers
  // For listing: internal_id = property.id
  // For user: internal_id = person.id (or buyer.id)
  let gqlQuery, resultKey, idField;
  
  if (table === 'external_user') {
    gqlQuery = 'query FindBuyerByInternalId($id: UUID) { buyers(filter: { id: { eq: $id } }, first: 1) { edges { node { id } } } }';
    resultKey = 'buyers';
    idField = supabaseUuid; // direct UUID match on buyer.id
  } else if (table === 'listing') {
    gqlQuery = 'query FindPropertyByInternalId($id: UUID) { properties(filter: { id: { eq: $id } }, first: 1) { edges { node { id } } } }';
    resultKey = 'properties';
    idField = supabaseUuid;
  } else if (table === 'user') {
    gqlQuery = 'query FindPersonByInternalId($id: UUID) { people(filter: { id: { eq: $id } }, first: 1) { edges { node { id } } } }';
    resultKey = 'people';
    idField = supabaseUuid;
  } else {
    return null;
  }

  try {
    const data = await gql(gqlQuery, { id: idField });
    const edges = data?.[resultKey]?.edges || [];
    if (edges.length > 0) {
      const internalId = edges[0].node.id;
      internalIdCache.set(cacheKey, { internalId, ts: Date.now() });
      return internalId;
    }
  } catch (e) {
    console.log(`  [Lookup] Failed for ${table}/${supabaseUuid}: ${e.message}`);
  }
  return null;
}

// Fetch user phone from Supabase by user_id UUID (user table = authenticated website users)
async function fetchSupabaseUser(userId) {
  return fetchSupabaseRecord('user', userId);
}

// Fetch external user phone from Supabase by external_user_id UUID (non-authenticated users)
async function fetchSupabaseExternalUser(extUserId) {
  return fetchSupabaseRecord('external_user', extUserId);
}

// Generic Supabase record fetcher
async function fetchSupabaseRecord(table, id) {
  if (!id || !SUPABASE_URL || !SUPABASE_SERVICE_KEY) return null;
  try {
    const select = table === 'offer'
      ? 'id,user_id,external_user_id,listing_id,offer_price,offer_note,counter_offer_note,final_note,internal_id'
      : 'id,name,phone_number,email';
    const url = `${SUPABASE_URL}/rest/v1/${table}?id=eq.${id}&select=${select}`;
    const res = await fetch(url, {
      headers: {
        'apikey': SUPABASE_SERVICE_KEY,
        'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}`,
      },
    });
    if (!res.ok) return null;
    const data = await res.json();
    return data?.[0] || null;
  } catch (e) {
    console.log(`  [SupabaseRecord] Fetch failed for ${table}/${id}: ${e.message}`);
    return null;
  }
}

// Write back to Supabase via REST API
async function supabasePatch(table, id, updates) {
  if (!SUPABASE_URL || !SUPABASE_SERVICE_KEY) {
    throw new Error('Supabase credentials not configured');
  }
  const url = `${SUPABASE_URL}/rest/v1/${table}?id=eq.${id}`;
  const res = await fetch(url, {
    method: 'PATCH',
    headers: {
      'apikey': SUPABASE_SERVICE_KEY,
      'Authorization': `Bearer ${SUPABASE_SERVICE_KEY}`,
      'Content-Type': 'application/json',
      'Prefer': 'return=minimal',
    },
    body: JSON.stringify(updates),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`Supabase PATCH failed: ${res.status} ${text}`);
  }
}

// ============================================
// ROUTES
// ============================================

function validRelaySecret(req) {
  if (!NINETYNINE_ACRES_RELAY_SECRET) return false;
  const supplied = req.headers['x-jumbo-relay-secret'];
  return typeof supplied === 'string' && supplied.length === NINETYNINE_ACRES_RELAY_SECRET.length &&
    crypto.timingSafeEqual(Buffer.from(supplied), Buffer.from(NINETYNINE_ACRES_RELAY_SECRET));
}

app.post('/api/99acres/post-listing', async (req, res) => {
  if (!validRelaySecret(req)) return res.status(401).json({ error: 'Unauthorized' });
  if (!NINETYNINE_ACRES_TOKEN) return res.status(503).json({ error: '99acres relay token is not configured' });

  const payload = req.body && typeof req.body === 'object' ? { ...req.body } : null;
  if (!payload) return res.status(400).json({ error: 'JSON object required' });

  // Validate the fields 99acres requires before spending a post attempt.
  const required = ['City', 'Locality', 'Prop_Name', 'Latitude', 'Longitude', 'Price', 'Description'];
  const missing = required.filter((key) => payload[key] === undefined || payload[key] === null || String(payload[key]).trim() === '');
  if (missing.length) return res.status(422).json({ error: 'Missing required listing fields', fields: missing });
  if (String(payload.Description).length < 30) return res.status(422).json({ error: 'Description must be at least 30 characters' });

  // Build this value as a JSON string on the relay, not by interpolating nested JSON in Twenty.
  const parking = payload.Reserved_Parking;
  payload.Reserved_Parking = JSON.stringify(parking && typeof parking === 'object' ? parking : { C: 1, O: 0 });
  if (Array.isArray(payload.PhotosData)) {
    payload.PhotosData = payload.PhotosData
      .filter((photo) => photo && typeof photo.path === 'string' && photo.path.startsWith('http'))
      .map((photo) => typeof photo === 'string' ? photo : JSON.stringify(photo));
  }

  try {
    const upstream = await fetch('https://www.99acres.com/99api/v21/listing/qwerty?rtype=json', {
      method: 'POST',
      headers: {
        Accept: 'application/json',
        'Content-Type': 'application/json',
        'Accept-Encoding': 'application/json',
        Authorization: `Bearer ${NINETYNINE_ACRES_TOKEN}`,
      },
      body: JSON.stringify(payload),
    });
    const text = await upstream.text();
    let result;
    try { result = JSON.parse(text); } catch { result = { raw: text }; }
    console.log(`[99acres-relay] upstream=${upstream.status} prop=${result?.prop_id || result?.error?.errorMsg || 'unknown'}`);
    if (result?.status === true && result?.prop_id) return res.status(200).json({ upstreamStatus: upstream.status, ...result });
    if (result?.error?.errorCode === 5) return res.status(409).json({ upstreamStatus: upstream.status, ...result });
    if (result?.error?.errorCode === 2 || result?.error?.errorCode === 10) return res.status(502).json({ upstreamStatus: upstream.status, ...result });
    return res.status(422).json({ upstreamStatus: upstream.status, ...result });
  } catch (err) {
    console.error('[99acres-relay] request failed:', err.message);
    return res.status(502).json({ error: '99acres request failed' });
  }
});

app.get('/healthz', (req, res) => {
  refillTokens();
  res.status(200).json({
    ok: true, uptime: process.uptime(), queueDepth: WORK_QUEUE.length, activeTasks: activeWorkers,
    tokenSet: !!TOKEN, kapsoSecretSet: !!KAPSO_WEBHOOK_SECRET,
    rateLimiter: { tokensLeft: Math.floor(tokens), maxTokens: RATE_LIMIT, utilization: Math.round(((RATE_LIMIT - tokens) / RATE_LIMIT) * 100) },
    stats: { ...stats },
    reassignmentQueue: REASSIGNMENT_QUEUE.length,
  });
});

app.get('/health', (req, res) => {
  res.json({ ok: true, tokenSet: !!TOKEN, kapsoSecretSet: !!KAPSO_WEBHOOK_SECRET, queueDepth: WORK_QUEUE.length, stats: { ...stats }, reassignmentQueue: REASSIGNMENT_QUEUE.length });
});

// Alias: Housing and other portals send to /enquiries/:source
app.post('/enquiries/:source', async (req, res) => { req.url = '/api/' + req.url.slice(1); app.handle(req, res); });
app.post('/api/enquiries/:source', async (req, res) => {
  const source = req.params.source;
  console.log(`[${source}] Source IP: ${getClientIp(req)}`);
  let payload;
  try {
    if (source === '99acres') payload = normalize99Acres(req.body);
    else if (source === 'housing') payload = normalizeHousing(req.body);
    else if (source === 'jumbo') payload = normalizeJumbo(req.body);
    else return res.status(400).json({ error: 'Unknown source' });
    if (!payload.phoneDigits) return res.status(400).json({ error: 'Phone number missing' });

    // Reserve before the async queue starts. This is the source-level kill switch:
    // duplicates never enter the CRM and therefore cannot trigger Kapso/WhatsApp.
    const reservation = await reservePortalEnquiry(source, payload.phoneDigits, payload.listingId);
    if (reservation.duplicate) {
      console.log(`[${source}] Skipped duplicate before CRM creation (listing=${String(payload.listingId).toUpperCase()})`);
      return res.status(202).json({ accepted: true, source, deduplicated: true });
    }
    payload._dedupKey = reservation.key;

    stats.accepted++;
    res.status(202).json({ accepted: true, source });
    console.log(`[${source}] Queued enquiry from ${payload.phoneDigits} (queue: ${WORK_QUEUE.length})`);
    enqueueWork(() => processPortalEnquiry(source, payload))
      .then(result => { stats.completed++; console.log(`[${source}] Completed: enquiry ${result.enquiry.id}`); })
      .catch(async err => {
        stats.failed++;
        await releasePortalEnquiry(payload._dedupKey).catch(releaseErr => console.error(`[${source}] Dedup release failed:`, releaseErr.message));
        console.error(`[${source}] Failed:`, err.message);
      });
  } catch (err) {
    console.error(`[${source}] Error:`, err.message);
    if (!res.headersSent) res.status(err.isRateLimit ? 503 : 500).json({ error: err.message });
  }
});

// Source IP logging helper — Caddy sets X-Forwarded-For with the real client IP
function getClientIp(req) {
  const forwarded = req.headers['x-forwarded-for'];
  if (forwarded) return forwarded.split(',')[0].trim();
  return req.ip || req.socket?.remoteAddress || 'unknown';
}

// --- KAPSO WHATSAPP (Conversation Model) ---
function verifyKapsoSignature(req) {
  if (!KAPSO_WEBHOOK_SECRET) return true;
  const sig = req.headers['x-webhook-signature'];
  if (!sig) return false;
  const expected = crypto.createHmac('sha256', KAPSO_WEBHOOK_SECRET).update(req.rawBody).digest('hex');
  return crypto.timingSafeEqual(Buffer.from(sig), Buffer.from(expected));
}

const KAPSO_PROJECT_ID = process.env.KAPSO_PROJECT_ID || '6c8c7064-840f-436d-8d28-89c8e1751052';
const KAPSO_INBOX_URL = `https://inbox.kapso.ai/projects/${KAPSO_PROJECT_ID}`;

async function createCommunicationRecord({ personId, enquiryId, direction, summary, rawMessage, messageId, deliveryStatus, timestamp, name }) {
  try {
    const id = require('crypto').randomUUID();
    const safeName = (name || 'WhatsApp: Unknown').replace(/'/g, "''");
    const safeSummary = (summary || '').substring(0, 255).replace(/'/g, "''");
    const safeRawMessage = (rawMessage || '').replace(/'/g, "''");
    const safeMessageId = (messageId || '').replace(/'/g, "''");
    const safeDelivery = (deliveryStatus || 'SENT').replace(/'/g, "''");
    const ts = timestamp || new Date().toISOString();

    const sql = `INSERT INTO workspace_1l3urgumjmspnjxohclmfz6fx._communication (
      id, name, "communicationType", direction, summary, "rawMessage", timestamp,
      "personId", "enquiryId", "messageId", "deliverystatus", "createdBySource", "createdAt", "updatedAt", position,
      "callLinkPrimaryLinkUrl", "callLinkPrimaryLinkLabel"
    ) VALUES (
      '${id}', '${safeName}', 'WHATSAPP', '${direction}', '${safeSummary}', '${safeRawMessage}',
      '${ts}'::timestamptz, ${personId ? `'${personId}'` : 'NULL'}, ${enquiryId ? `'${enquiryId}'` : 'NULL'},
      '${safeMessageId}', '${safeDelivery}', 'API', NOW(), NOW(), 0,
      '${KAPSO_INBOX_URL}', 'Open in Kapso'
    ) RETURNING id`;

    const result = await execDockerPsql(sql);
    return result.trim() || null;
  } catch (err) { console.error('[Communication] Create failed:', err.message); return null; }
}

async function appendToConversation(recordId, newMessage, timestamp) {
  try {
    const safeMsg = newMessage.replace(/'/g, "''");
    const ts = timestamp || new Date().toISOString();
    const sql = `UPDATE workspace_1l3urgumjmspnjxohclmfz6fx._communication
      SET "rawMessage" = COALESCE("rawMessage", '') || E'\n---\n' || '${safeMsg}',
          timestamp = '${ts}'::timestamptz,
          "updatedAt" = NOW()
      WHERE id = '${recordId}'`;
    await execDockerPsql(sql);
    return 'appended';
  } catch (err) { console.error('[Communication] Append failed:', err.message); return null; }
}

async function generateSummary(rawMessage) {
  try {
    const env = process.env;
    const resp = await fetch('https://openrouter.ai/api/v1/chat/completions', {
      method: 'POST',
      headers: { 'Authorization': 'Bearer ' + env.OPENROUTER_API_KEY, 'Content-Type': 'application/json' },
      body: JSON.stringify({
        model: 'openai/gpt-4o-mini',
        messages: [{ role: 'user', content: `Summarize this WhatsApp conversation between a real estate agent and a potential buyer in 1-2 sentences. Focus on: what the buyer is looking for (budget, location, BHK), their stage in the buying process, and any action items.\n\nConversation:\n${rawMessage.substring(0, 4000)}` }],
        max_tokens: 150
      })
    });
    const data = await resp.json();
    return data?.choices?.[0]?.message?.content?.trim() || '';
  } catch (err) { console.error('[Summary] LLM failed:', err.message); return ''; }
}

async function findExistingConversation(personId, direction) {
  if (!personId) return null;
  const today = new Date().toISOString().split('T')[0];
  try {
    // Use direct DB query (custom object GraphQL filters are unreliable)
    const query = `SELECT id, name, \"rawMessage\", timestamp, \"deletedAt\", \"communicationType\", direction, \"personId\"
      FROM workspace_1l3urgumjmspnjxohclmfz6fx._communication
      WHERE \"personId\" = '${personId}'
        AND \"communicationType\" = 'WHATSAPP'
        AND direction = '${direction}'
        AND \"deletedAt\" IS NULL
        AND DATE(timestamp) = '${today}'
      ORDER BY timestamp DESC
      LIMIT 1`;
    const result = await execDockerPsql(query);
    if (result.trim()) {
      // Parse the pipe-delimited output
      const parts = result.trim().split('|');
      if (parts.length >= 4) {
        return {
          id: parts[0],
          name: parts[1],
          rawMessage: parts[2],
          timestamp: parts[3],
          deletedAt: parts[4] || null,
          communicationType: parts[5],
          direction: parts[6],
          personId: parts[7]
        };
      }
    }
    return null;
  } catch (err) { console.error('[Communication] Find existing FAILED:', err.message); return null; }
}

function execDockerPsql(sql) {
  const { execSync } = require('child_process');
  const escapedSql = sql.replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  const cmd = `docker exec twenty-db-1 psql -U twenty -d default -t -A -c "${escapedSql}"`;
  try {
    const result = execSync(cmd, { encoding: 'utf8', timeout: 10000 });
    return result.trim();
  } catch (e) {
    console.error('[execDockerPsql] ERROR:', e.message);
    console.error('[execDockerPsql] CMD:', cmd);
    return '';
  }
}

async function updateCommunicationStatus(messageId, status) {
  try { await gql(`mutation UpdateCommStatus($filter: CommunicationFilterInput!, $input: CommunicationUpdateInput!) { updateCommunications(filter: $filter, data: $input) { id } }`, { filter: { messageId: { eq: messageId } }, input: { deliveryStatus: status } }); }
  catch (err) { console.error('[Kapso] Status update failed:', err.message); }
}

async function findOpenEnquiryForPerson(personId) {
  if (!personId) return null;
  const data = await gql(`query FindOpenEnquiry($personId: UUID) { enquiries(filter: { buyer: { personId: { eq: $personId } }, statusDetail: { in: ["NEW_LEAD", "CONTACTED", "MORE_OPTIONS_REQUESTED"] } }, first: 1, orderBy: { createdAt: DescNullsLast }) { edges { node { id enquiryNumber statusDetail } } } }`, { personId });
  const edges = data?.enquiries?.edges || [];
  return edges.length ? edges[0].node : null;
}

async function updateEnquiryStatus(enquiryId, newStatus) {
  if (!enquiryId || !newStatus) return;
  try {
    const data = await gql(`mutation UpdateEnquiryStatus($id: ID!, $input: EnquiryUpdateInput!) { updateEnquiry(id: $id, data: $input) { id statusDetail } }`, { id: enquiryId, input: { statusDetail: newStatus } });
    console.log(`  [Enquiry] Status updated: ${enquiryId} -> ${data?.updateEnquiry?.statusDetail}`);
  } catch (err) { console.error(`  [Enquiry] Status update failed for ${enquiryId}:`, err.message); }
}

function buildConversationName(personName, direction, timestamp) {
  const date = new Date(timestamp);
  const dateStr = date.toLocaleDateString('en-GB', { day: 'numeric', month: 'short' });
  const name = personName || 'Unknown';
  return `💬 ${name} x Ananya - ${dateStr}`;
}

async function handleSingleEvent({ event, message, conversation }) {
  if (conversation?.phone_number) {
    const normalizedPhone = normalizePhone(conversation.phone_number);
    if (!normalizedPhone) return;
    const person = await findPersonByPhone(normalizedPhone);

    if (event === 'whatsapp.message.received' || event === 'message.received' || event === 'message.inbound') {
      const content = message?.kapso?.content || message?.text?.body || `[${message?.type || 'unknown'}]`;
      const timestamp = message?.timestamp ? new Date(parseInt(message.timestamp) * 1000).toISOString() : new Date().toISOString();
      const personName = person?.nameFirstName ? (person.nameFirstName + (person.nameLastName ? ' ' + person.nameLastName : '')) : null;

      // Check for existing conversation today
      const existing = person?.id ? await findExistingConversation(person.id, 'INBOUND') : null;
      console.log(`[Kapso] findExistingConversation result: ${existing ? existing.id : 'null'}`);

      if (existing) {
        // Append to existing conversation
        const msgFormatted = `[${new Date(timestamp).toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })}] INBOUND: ${content}`;
        const updatedRaw = await appendToConversation(existing.id, msgFormatted, timestamp);
        console.log(`[Kapso] Appended to conversation ${existing.id} for ${normalizedPhone}`);
        // Regenerate summary if we have the updated raw message
        if (updatedRaw) {
          const summary = await generateSummary(updatedRaw);
          if (summary) {
            try {
              await gql(`mutation UpdateCommSummary($id: UUID!, $input: CommunicationUpdateInput!) { updateCommunication(id: $id, data: $input) { id } }`, { id: existing.id, input: { summary } });
            } catch (err) { console.error('[Summary] Update failed:', err.message); }
          }
        }
      } else {
        // Create new conversation record
        const openEnquiry = person?.id ? await findOpenEnquiryForPerson(person.id) : null;
        if (openEnquiry) console.log(`[Kapso] Found open enquiry ${openEnquiry.id} (${openEnquiry.statusDetail}) for person ${person.id}`);

        const convName = buildConversationName(personName, 'INBOUND', timestamp);
        const msgFormatted = `[${new Date(timestamp).toLocaleString('en-GB', { timeZone: 'Asia/Kolkata' })}] INBOUND: ${content}`;
        const summary = await generateSummary(msgFormatted);

        const newId = await createCommunicationRecord({
          personId: person?.id, enquiryId: openEnquiry?.id || null, direction: 'INBOUND',
          summary, rawMessage: msgFormatted, messageId: message?.id,
          deliveryStatus: 'SENT', timestamp, name: convName,
        });
        console.log(`[Kapso] Created conversation ${newId} for ${normalizedPhone}`);

        if (openEnquiry && openEnquiry.statusDetail === 'NEW_LEAD') {
          console.log(`[Kapso] Enquiry ${openEnquiry.id} is NEW_LEAD -> updating to CONTACTED`);
          await updateEnquiryStatus(openEnquiry.id, 'CONTACTED');
        }
      }
    }

    if ((event === 'whatsapp.message.sent' || event === 'message.sent') && message?.id) {
      if (conversation?.phone_number) {
        const normalizedPhone = normalizePhone(conversation.phone_number);
        const person = await findPersonByPhone(normalizedPhone);
        const openEnquiry = person?.id ? await findOpenEnquiryForPerson(person.id) : null;
        if (openEnquiry && openEnquiry.statusDetail === 'NEW_LEAD') {
          console.log(`[Kapso] Outbound to ${normalizedPhone}: enquiry ${openEnquiry.id} NEW_LEAD -> CONTACTED`);
          await updateEnquiryStatus(openEnquiry.id, 'CONTACTED');
        }
      }
    }
  }
  if (message?.id) {
    if (event === 'whatsapp.message.sent' || event === 'message.sent') await updateCommunicationStatus(message.id, 'SENT');
    else if (event === 'whatsapp.message.delivered' || event === 'message.delivered') await updateCommunicationStatus(message.id, 'DELIVERED');
    else if (event === 'whatsapp.message.read' || event === 'message.read') await updateCommunicationStatus(message.id, 'READ');
    else if (event === 'whatsapp.message.failed' || event === 'message.failed') await updateCommunicationStatus(message.id, 'FAILED');
  }
}

app.post('/api/whatsapp/inbound', async (req, res) => {
  console.log(`[Kapso] Source IP: ${getClientIp(req)}`);
  if (!verifyKapsoSignature(req)) return res.status(401).json({ error: 'Invalid signature' });
  res.json({ success: true });
  try {
    const batchEvents = req.body.batch && Array.isArray(req.body.data)
      ? req.body.data.map(item => ({ event: req.body.type, message: item.message, conversation: item.conversation }))
      : [{ event: req.body.event || req.body.type, message: req.body.message, conversation: req.body.conversation }];
    for (const ev of batchEvents) await handleSingleEvent(ev);
  } catch (err) { console.error('[Kapso] Handler error:', err.message); }
});

// --- SUPABASE WEBHOOK ROUTES ---

async function handleSupabaseWebhook(req, res, handler) {
  const routeName = req.path.split('/').pop();
  console.log(`[Supabase/${routeName}] Source IP: ${getClientIp(req)}`);
  const { type, record } = req.body;
  if (!record?.id) return res.status(400).json({ error: 'Missing record' });
  if (type !== 'INSERT') return res.status(200).json({ ok: true, skipped: true, reason: `type=${type}` });

  res.status(202).json({ accepted: true });
  console.log(`[Supabase/${req.params.type}] Queued: ${record.id}`);
  enqueueWork(() => handler(record))
    .then(result => console.log(`[Supabase/${req.params.type}] Done ${record.id}:`, JSON.stringify(result)))
    .catch(async err => {
      if (req.params.type === 'visit') {
        await saveVisitRetry(record, err).catch(saveErr => console.error(`[Supabase/visit-retry] Save failed ${record.id}:`, saveErr.message));
      }
      console.error(`[Supabase/${req.params.type}] Failed ${record.id}:`, err.message);
    });
}

app.post('/api/supabase/web_signup', (req, res) => handleSupabaseWebhook(req, res, handleWebSignup));
app.post('/api/supabase/seller_signup', (req, res) => handleSupabaseWebhook(req, res, handleSellerSignup));
app.post('/api/supabase/visit', (req, res) => handleSupabaseWebhook(req, res, handleVisit));
app.post('/api/supabase/offer', (req, res) => handleSupabaseWebhook(req, res, handleOffer));

// --- ERROR HANDLING ---
process.on('unhandledRejection', (err) => { console.error('[FATAL] Unhandled rejection:', err?.message || err); });
process.on('uncaughtException', (err) => { console.error('[FATAL] Uncaught exception:', err?.message || err); });

app.listen(PORT, async () => {
  console.log(`Jumbo Webhook Proxy v5 running on port ${PORT}`);
  console.log(`Twenty API: ${TWENTY_API_URL}`);
  console.log(`Token set: ${!!TOKEN}`);
  console.log(`Kapso secret set: ${!!KAPSO_WEBHOOK_SECRET}`);
  console.log(`Rate limiter: ${RATE_LIMIT} req/min token bucket (${Math.round(REFILL_RATE * 1000)}/s refill)`);
  console.log(`Async queue: max ${MAX_WORKERS} workers, 202 accepted immediately, ${REQUEST_TIMEOUT_MS}ms per-request budget`);
  console.log(`Aashish workspace member: ${AASHISH_WORKSPACE_MEMBER_ID}`);
  console.log(`Reassignment delay: ${REASSIGNMENT_DELAY_MS / 60000} minutes`);
  console.log(`Zone resolution: building.zoneId -> active ZoneAgent allocation`);
  setInterval(processVisitRetryQueue, 60000);
  setTimeout(processVisitRetryQueue, 5000);
});
