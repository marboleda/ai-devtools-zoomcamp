# Restaurant Waitlist Manager — Product & Technical Specification

## 1. Product Overview

Restaurant Waitlist Manager is a multi-tenant application that allows restaurants to manage their dining tables and customer waitlists.

Each restaurant has its own account and isolated data. Restaurant employees can configure tables, manage table availability, add customers to the waitlist, assign customers to tables, and free occupied tables.

Customers may join a restaurant's waitlist through a public-facing interface when enabled by the restaurant. Customers provide their party size and phone number and receive an SMS notification when a suitable table becomes available.

The system automatically matches waiting parties to available tables using a first-come, first-served policy.

---

## 2. Goals

The application should:

1. Allow restaurants to create and manage their own accounts.
2. Allow restaurants to configure their tables and table capacities.
3. Track whether each table is available or occupied.
4. Allow employees to add customers to the waitlist.
5. Optionally allow customers to join the waitlist themselves.
6. Match waiting parties to appropriate available tables.
7. Maintain first-come, first-served ordering.
8. Notify customers by SMS when their table becomes available.
9. Ensure only authorized restaurant employees can mark tables as available.
10. Keep each restaurant's data completely isolated from other restaurants.

---

## 3. User Roles

### 3.1 Restaurant Owner / Administrator

The restaurant administrator can:

- Create a restaurant account.
- Configure restaurant information.
- Configure tables.
- Add, remove, and modify tables.
- Set table capacities.
- Create and manage employee accounts.
- Enable or disable public waitlist access.
- View the current waitlist.
- View table availability.
- Manually assign customers to tables.
- Remove customers from the waitlist.
- Mark tables as occupied/available.
- View waitlist history.

### 3.2 Restaurant Employee

Employees can:

- Log into the restaurant's dashboard.
- View tables.
- View the waitlist.
- Add customers to the waitlist.
- Remove customers from the waitlist.
- Assign customers to tables.
- Mark occupied tables as available.
- View customer contact information necessary for waitlist management.

Employees cannot:

- Modify restaurant ownership.
- Manage billing/subscription information.
- Delete the restaurant.
- Modify administrator permissions unless explicitly authorized.

### 3.3 Customer

Customers can:

- Join a restaurant's waitlist if public access is enabled.
- Provide their name.
- Provide party size.
- Provide phone number.
- View their position/status on the waitlist.
- Receive an SMS notification when a table becomes available.
- Optionally cancel their waitlist entry.

Customers cannot:

- Modify tables.
- Modify restaurant settings.
- Mark tables as available.
- Modify another customer's waitlist entry.
- Assign themselves to a table.

---

## 4. Restaurant Account

Each restaurant is a separate tenant.

A restaurant account should contain:

- Restaurant ID
- Restaurant name
- Address
- Phone number
- Time zone
- Account owner
- Public waitlist enabled/disabled
- Created date
- Account status

All restaurant-owned resources must reference a `restaurant_id`.

A user belonging to Restaurant A must never be able to access Restaurant B's tables, customers, or waitlist.

---

## 5. Table Management

Restaurants can configure their physical tables within the application.

Each table should have:

- Table ID
- Restaurant ID
- Table name/number
- Capacity
- Status
- Optional location/section

Example:

| Table | Capacity | Status |
|---|---:|---|
| 1 | 2 | Available |
| 2 | 2 | Occupied |
| 3 | 4 | Available |
| 4 | 4 | Occupied |
| 5 | 6 | Available |
| 6 | 8 | Available |

### Table statuses

A table should have at least:

- `AVAILABLE`
- `OCCUPIED`

Future versions could introduce:

- `AVAILABLE`
- `OCCUPIED`
- `RESERVED`
- `OUT_OF_SERVICE`

---

## 6. Waitlist Entry

A waitlist entry represents one customer party waiting for a table.

Required fields:

- Waitlist entry ID
- Restaurant ID
- Customer name
- Party size
- Phone number
- Created timestamp
- Status
- Assigned table
- Notification status

Example:

```text
Waitlist Entry
------------------------------
ID:             18372
Restaurant:     42
Customer:       John Smith
Party Size:     4
Phone:          +1XXXXXXXXXX
Created:        10:42:31 AM
Status:         WAITING
Assigned Table: NULL
Notification:   NOT_SENT
```

---

## 7. Waitlist Statuses

A waitlist entry should have a lifecycle.

```text
WAITING
   |
   v
ASSIGNED
   |
   v
SEATED
```

Other possible terminal states:

- `CANCELLED`
- `NO_SHOW`
- `EXPIRED`

For the MVP, the minimum required states are:

- `WAITING`
- `ASSIGNED`
- `CANCELLED`
- `SEATED`

---

## 8. Joining the Waitlist

There should be two methods of joining.

### Employee-Assisted

An employee enters:

- Customer name
- Party size
- Phone number

The system immediately evaluates whether an appropriate table is available.

### Public-Facing

If public waitlist access is enabled, a customer can access a restaurant-specific page.

Example:

```text
restaurantwaitlist.com/r/restaurant-name
```

The customer enters:

```text
Name: John Smith
Party size: 4
Phone number: +1 XXX XXX XXXX
```

The system then attempts to assign an appropriate table.

---

## 9. Table Assignment Algorithm

The central business rule is:

> A waiting party should be assigned to the smallest available table whose capacity can accommodate the entire party.

For example:

```text
Available tables:

Table 1 → capacity 2
Table 2 → capacity 4
Table 3 → capacity 6
Table 4 → capacity 8
```

A party of 4 should be assigned to Table 2 rather than Table 3 or Table 4.

The algorithm is:

```text
Find available tables
    ↓
Filter tables where capacity >= party_size
    ↓
Sort by capacity ascending
    ↓
Select smallest suitable table
```

This minimizes wasted seating capacity.

---

## 10. Waitlist Ordering

Waitlist entries are ordered by arrival time.

Example:

| Position | Customer | Party Size | Time |
|---:|---|---:|---|
| 1 | John | 2 | 6:01 |
| 2 | Sarah | 4 | 6:03 |
| 3 | Mike | 2 | 6:05 |
| 4 | Lisa | 6 | 6:07 |

The system should use a timestamp generated by the server rather than trusting the client's timestamp.

---

## 11. Important Matching Rule

The waitlist must preserve **first-come, first-served ordering while respecting table capacity**.

For example:

Available table:

```text
Table 5 → capacity 4
```

Waitlist:

```text
1. Party A → 6 people
2. Party B → 4 people
3. Party C → 2 people
```

Party A cannot use the table.

Therefore Party B receives the table.

Party C remains waiting.

This means the system should not simply process the entire queue sequentially and stop at the first incompatible party.

Instead:

```text
For each waiting party in chronological order:
    Find a suitable available table.

    If suitable table exists:
        assign table
        continue checking remaining parties
```

However, this rule creates an important product decision: whether a later smaller party may take a table that could accommodate an earlier larger party if a different table becomes available shortly afterward.

For the MVP, the recommended behavior is **immediate assignment**: once a suitable table becomes available, assign it to the earliest waiting party that can use it.

---

## 12. Automatic Assignment

When a table becomes available, the system should automatically evaluate the waitlist.

Example:

```text
Current tables:

Table 1 → 2 → Occupied
Table 2 → 4 → Available
Table 3 → 6 → Occupied
```

Waitlist:

```text
1. Alice → 6 people
2. Bob → 4 people
3. Carol → 2 people
```

Table 2 becomes available.

The system checks:

```text
Alice → needs 6 → cannot use Table 2
Bob   → needs 4 → can use Table 2
```

Therefore:

```text
Table 2 → Bob
```

Bob's waitlist entry changes to:

```text
ASSIGNED
```

Alice and Carol remain waiting.

---

## 13. Table Lifecycle

Only restaurant employees can change a table from occupied to available.

Example:

```text
AVAILABLE
    |
    | Customer seated
    v
OCCUPIED
    |
    | Employee frees table
    v
AVAILABLE
```

A customer cannot free a table.

The backend must enforce this authorization rather than relying solely on frontend controls.

---

## 14. Customer Notification

When a waitlist entry receives a table assignment, the customer should receive an SMS notification.

Example:

```text
Your table at The Restaurant is ready.

Please check in with the host within 10 minutes.
```

The notification should be sent only after the table assignment has been successfully committed.

The system should record:

- Notification ID
- Waitlist entry ID
- Phone number
- Notification type
- Sent timestamp
- Delivery status
- Provider message ID
- Error information, if applicable

Example statuses:

- `PENDING`
- `SENT`
- `DELIVERED`
- `FAILED`

---

## 15. Notification Failure

If an SMS provider fails to send a notification:

1. The table assignment should remain valid.
2. The system records the failure.
3. The employee dashboard should indicate the notification failure.
4. The system may retry the notification.

The table should not automatically become available again simply because SMS delivery failed.

---

## 16. Notification Timeout

The restaurant should optionally be able to configure how long a customer has to claim their table.

Example:

```text
Table assigned
     ↓
SMS sent
     ↓
10-minute timer
     ↓
Customer checks in
     ↓
SEATED
```

If the customer does not show up:

```text
ASSIGNED
    ↓
NO_SHOW
    ↓
Table becomes AVAILABLE
```

This functionality could be implemented after the MVP.

---

## 17. Employee Dashboard

The primary employee interface should display two major areas.

### Tables

Example:

```text
TABLES

┌─────────┬──────────┬────────────┐
│ Table 1 │ 2 seats  │ AVAILABLE  │
│ Table 2 │ 4 seats  │ OCCUPIED   │
│ Table 3 │ 4 seats  │ AVAILABLE  │
│ Table 4 │ 6 seats  │ OCCUPIED   │
│ Table 5 │ 8 seats  │ AVAILABLE  │
└─────────┴──────────┴────────────┘
```

Employees should be able to mark an occupied table as available.

### Waitlist

Example:

```text
WAITLIST

1. John Smith
   Party of 4
   Waiting 18 min
   SMS

2. Jane Doe
   Party of 2
   Waiting 14 min
   SMS

3. Robert Brown
   Party of 6
   Waiting 7 min
   SMS
```

The interface should clearly indicate when a customer has been assigned a table.

---

## 18. Restaurant Configuration

Restaurant administrators should be able to configure:

### Restaurant Information

- Name
- Address
- Phone
- Time zone

### Waitlist

- Public waitlist enabled/disabled
- Maximum party size
- SMS notifications enabled/disabled
- Estimated notification timeout

### Tables

- Table number/name
- Capacity
- Section/location
- Active/inactive

---

## 19. Authentication

The application should use authenticated accounts for restaurant employees.

Recommended model:

```text
User
 |
 +-- Restaurant Membership
        |
        +-- Role
```

Possible roles:

- `OWNER`
- `MANAGER`
- `EMPLOYEE`

Customers do not necessarily need accounts for the MVP.

Instead, they can join the waitlist using their name, party size, and phone number.

This reduces friction for customers.

---

## 20. Data Model

A relational database such as PostgreSQL is well suited to this application.

### `restaurants`

```text
id
name
address
phone
timezone
public_waitlist_enabled
created_at
updated_at
```

### `users`

```text
id
email
password_hash
name
created_at
updated_at
```

### `restaurant_users`

```text
id
restaurant_id
user_id
role
created_at
```

### `tables`

```text
id
restaurant_id
name
capacity
status
created_at
updated_at
```

### `waitlist_entries`

```text
id
restaurant_id
customer_name
party_size
phone_number
status
assigned_table_id
created_at
assigned_at
seated_at
cancelled_at
updated_at
```

### `notifications`

```text
id
waitlist_entry_id
type
phone_number
status
provider_message_id
sent_at
delivered_at
failed_at
error_message
created_at
```

### `table_events`

A table event/history table is recommended.

```text
id
table_id
previous_status
new_status
changed_by_user_id
created_at
```

This provides an audit trail of table status changes.

---

## 21. API Design

A REST API would be sufficient for the initial version.

### Authentication

```http
POST /api/auth/register
POST /api/auth/login
POST /api/auth/logout
```

### Restaurants

```http
GET    /api/restaurants/:restaurantId
PATCH  /api/restaurants/:restaurantId
```

### Tables

```http
GET    /api/restaurants/:restaurantId/tables
POST   /api/restaurants/:restaurantId/tables
PATCH  /api/tables/:tableId
DELETE /api/tables/:tableId

POST   /api/tables/:tableId/free
POST   /api/tables/:tableId/occupy
```

### Waitlist

```http
GET    /api/restaurants/:restaurantId/waitlist
POST   /api/restaurants/:restaurantId/waitlist
GET    /api/waitlist/:entryId
PATCH  /api/waitlist/:entryId
DELETE /api/waitlist/:entryId
POST   /api/waitlist/:entryId/cancel
POST   /api/waitlist/:entryId/seat
```

### Public Waitlist

A separate endpoint should be used for unauthenticated customers:

```http
GET  /api/public/restaurants/:restaurantSlug
POST /api/public/restaurants/:restaurantSlug/waitlist
GET  /api/public/waitlist/:publicToken
POST /api/public/waitlist/:publicToken/cancel
```

A customer should receive a random, unguessable token rather than exposing the database ID.

---

## 22. Transactional Assignment

Table assignment is a critical concurrency operation.

The application must prevent two customers from being assigned the same table.

For example:

```text
Employee A frees Table 4
        +
Employee B frees Table 4
        ↓
Two assignment operations execute simultaneously
```

The database transaction must guarantee that only one operation can claim the table.

Conceptually:

```text
BEGIN TRANSACTION

Lock available table

Find first compatible waiting party

Assign table to party

Change table status to OCCUPIED/ASSIGNED

Update waitlist entry

Create notification record

COMMIT
```

PostgreSQL row-level locking or an equivalent concurrency-control mechanism should be used.

---

## 23. Recommended Assignment State

There is an important distinction between a table being physically available and a table being assigned to a customer.

Therefore, a more robust table state model is:

- `AVAILABLE`
- `ASSIGNED`
- `OCCUPIED`

Flow:

```text
AVAILABLE
    ↓
ASSIGNED
    ↓
OCCUPIED
    ↓
AVAILABLE
```

`ASSIGNED` means the restaurant has assigned the table to a waiting customer, but the customer has not necessarily been seated yet.

This prevents the table from being assigned to another customer while the first customer is arriving.

---

## 24. Public Customer Experience

A customer-facing page could look like:

```text
--------------------------------
       THE RESTAURANT

       Join the Waitlist

Name
[________________]

Party size
[  4  ▼ ]

Phone number
[________________]

[ Join Waitlist ]
--------------------------------
```

After submission:

```text
You're on the waitlist!

Party size: 4
Position: #3

Estimated wait:
Approximately 25 minutes

We'll text you when your table is ready.

[Cancel Waitlist]
```

The exact wait estimate can initially be omitted if the application cannot reliably calculate it.

---

## 25. Employee Customer Experience

Employees should have a faster workflow:

```text
Add Customer

Name:        [____________]
Party Size:  [ 4 ]
Phone:       [____________]

[ Add to Waitlist ]
```

If a table is immediately available:

```text
Table 7 is available.

Capacity: 4

[Assign Table]
```

Alternatively, the system can automatically assign it without requiring employee confirmation.

For the MVP, automatic assignment is preferable.

---

## 26. Business Rules

The following rules should be enforced by the backend.

### Rule 1 — Tenant isolation

Users can only access resources belonging to their restaurant.

### Rule 2 — Capacity

A party may only be assigned to a table where:

```text
table.capacity >= party_size
```

### Rule 3 — Table availability

Only available tables may be assigned.

### Rule 4 — First come, first served

Earlier eligible waitlist entries take priority over later eligible entries.

### Rule 5 — Employee-only table freeing

Only authenticated restaurant employees with the appropriate restaurant membership may free a table.

### Rule 6 — Customer cancellation

Customers may cancel their own waitlist entry but may not modify another customer's entry.

### Rule 7 — Assignment uniqueness

A table cannot simultaneously be assigned to multiple parties.

### Rule 8 — Notification

An SMS notification is generated when a waitlist entry receives a table assignment.

---

## 27. Security Requirements

The application should include:

- Password hashing using a modern password-hashing algorithm.
- Secure session or token-based authentication.
- HTTPS.
- Authorization on every protected endpoint.
- Restaurant-level tenant isolation.
- Rate limiting on public waitlist endpoints.
- Input validation.
- Phone-number validation.
- Protection against SQL injection.
- Protection against XSS.
- CSRF protection where applicable.
- Audit logging for administrative actions.
- Unpredictable public customer tokens.

Phone numbers should be treated as private customer information and should not be unnecessarily exposed through public endpoints.

---

## 28. Audit Logging

Important actions should be recorded.

Examples:

```text
Employee added
Employee removed
Table created
Table capacity changed
Table marked occupied
Table freed
Customer added
Customer cancelled
Customer assigned to table
Customer seated
SMS sent
SMS failed
```

This is particularly useful when investigating disputes such as:

> "Why was this customer assigned before me?"

---

## 29. Notifications Architecture

SMS should be abstracted behind a notification service.

For example:

```text
Waitlist Service
       |
       v
Notification Service
       |
       v
SMS Provider
```

This prevents the rest of the application from being tightly coupled to a specific SMS provider.

The notification service could eventually support:

- SMS
- Email
- Push Notification

---

## 30. Architecture

A reasonable initial architecture would be:

```text
                    ┌───────────────────┐
                    │    Web Client     │
                    │                   │
                    │ Employee / Public │
                    └─────────┬─────────┘
                              │
                              │ HTTPS
                              ▼
                    ┌───────────────────┐
                    │    REST API       │
                    │                   │
                    │ Authentication    │
                    │ Restaurants       │
                    │ Tables            │
                    │ Waitlist          │
                    └─────────┬─────────┘
                              │
              ┌───────────────┼───────────────┐
              │               │               │
              ▼               ▼               ▼
        ┌───────────┐   ┌────────────┐  ┌──────────────┐
        │ PostgreSQL│   │ Job Queue  │  │ SMS Provider │
        └───────────┘   └────────────┘  └──────────────┘
```

A background worker should handle SMS delivery rather than making the employee/customer request wait for the SMS provider.

---

## 31. Suggested Technology Stack

A practical stack for the application could be:

### Frontend

- React
- TypeScript
- Next.js

### Backend

- Python
- FastAPI

or:

- TypeScript
- Node.js
- NestJS

### Database

- PostgreSQL

### Background Jobs

- Redis + Celery/RQ for Python

or:

- Redis + BullMQ for Node.js

### SMS

An SMS provider such as Twilio can be integrated behind the notification abstraction.

### Deployment

The application could initially be deployed using:

```text
Frontend → Vercel
API → Docker container
PostgreSQL → Managed PostgreSQL
Redis → Managed Redis
```

The exact infrastructure can be changed later without significantly changing the domain model.

---

## 32. MVP Scope

The first version should remain relatively small.

### Include

- Restaurant registration
- Employee authentication
- Restaurant configuration
- Employee management
- Table creation/editing/deletion
- Table capacities
- Table availability
- Employee waitlist entry
- Public waitlist
- Party size
- Phone number
- Automatic table matching
- First-come-first-served ordering
- Employee ability to free tables
- SMS notification
- Customer cancellation
- Basic waitlist/table dashboard
- Basic audit history

### Exclude initially

- Reservations
- Online payments
- POS integration
- Customer accounts
- Advanced analytics
- Table combinations
- Floor-plan editor
- Multiple restaurant locations
- Email notifications
- Push notifications
- Sophisticated wait-time prediction
- Loyalty programs

These can be added later.

---

## 33. Future Feature: Combining Tables

One important future requirement is supporting parties larger than any individual table.

For example:

```text
Table 1 → 4 seats
Table 2 → 4 seats
```

A party of 8 could potentially be seated at:

```text
Table 1 + Table 2 = 8 seats
```

This significantly complicates the assignment algorithm.

Therefore, table combinations should **not** be included in the initial MVP unless they are a core requirement.

---

## 34. Future Feature: Customer Tracking

Once a customer joins the waitlist, they could receive a private status page:

```text
WAITLIST STATUS

Restaurant: The Restaurant
Party: 4

Status:
Waiting

Position:
#3

Joined:
6:42 PM

[Cancel]
```

The customer could access this through a secure token in their SMS.

---

## 35. Future Feature: Estimated Wait Time

The system could eventually estimate wait times using historical data.

For example:

```text
Average seating time for party of 4: 58 minutes

Current parties ahead: 2

Estimated wait: 20–30 minutes
```

Later, this could incorporate:

- Party size
- Table size
- Historical table turnover
- Day of week
- Time of day
- Restaurant-specific averages

This should not be part of the initial assignment logic.

---

## 36. Future Feature: POS Integration

A restaurant may eventually want its POS system to automatically report when a table becomes available.

For example:

```text
POS
 │
 │ Table closed
 ▼
Waitlist Manager
 │
 ▼
Table AVAILABLE
 │
 ▼
Waitlist Matching
 │
 ▼
SMS
```

The architecture should therefore keep table status management separate from the UI so that external integrations can eventually update table status.

---

## 37. Core Use Cases

### Use Case 1 — Customer joins and table is available

```text
Customer joins
      ↓
Party size = 2
      ↓
System finds Table 3 (capacity 2)
      ↓
Table assigned
      ↓
Customer receives SMS
      ↓
Customer arrives
      ↓
Employee seats customer
```

### Use Case 2 — Customer joins and no table is available

```text
Customer joins
      ↓
Party size = 4
      ↓
No suitable table
      ↓
Customer added to waitlist
      ↓
Customer waits
```

### Use Case 3 — Table becomes available

```text
Employee frees Table 7
      ↓
Table capacity = 4
      ↓
System examines waitlist
      ↓
Find earliest party <= 4
      ↓
Assign Table 7
      ↓
Send SMS
```

### Use Case 4 — Customer cancels

```text
Customer cancels
      ↓
WAITING → CANCELLED
      ↓
Customer removed from active queue
```

### Use Case 5 — Employee frees a table

```text
Employee clicks "Free Table"
      ↓
Authorization check
      ↓
Table OCCUPIED → AVAILABLE
      ↓
Assignment algorithm runs
      ↓
Eligible waiting party assigned
```

---

## 38. Acceptance Criteria

The MVP should be considered complete when:

1. A restaurant can create an account.
2. An administrator can create tables with capacities.
3. Employees can authenticate.
4. Employees can view table availability.
5. Employees can add customers to the waitlist.
6. Customers can join the waitlist when public access is enabled.
7. A party is automatically assigned to a suitable available table.
8. The smallest suitable table is selected.
9. Parties without a suitable table are placed into the waitlist.
10. Waitlist ordering is based on server-generated arrival time.
11. When a table becomes available, the earliest eligible party is assigned.
12. Two parties cannot be assigned to the same table.
13. Only authorized employees can free tables.
14. Customers can cancel their own waitlist entries.
15. Customers receive an SMS after being assigned a table.
16. Failed SMS deliveries are recorded.
17. Restaurant data is isolated between tenants.
18. Important table and waitlist actions are auditable.

---

## 39. Recommended Development Phases

### Phase 1 — Foundation

- Project setup
- Database
- Authentication
- Restaurant accounts
- Employee roles
- Multi-tenant authorization

### Phase 2 — Table Management

- Table CRUD
- Capacities
- Table statuses
- Employee dashboard

### Phase 3 — Waitlist

- Waitlist CRUD
- Party size
- Automatic matching
- First-come-first-served logic
- Table assignment

### Phase 4 — Public Waitlist

- Restaurant public page
- Customer waitlist form
- Secure customer status token
- Customer cancellation

### Phase 5 — SMS

- SMS provider integration
- Background notification jobs
- Delivery tracking
- Retry handling

### Phase 6 — Hardening

- Concurrency testing
- Authorization testing
- Rate limiting
- Audit logging
- Error handling
- Automated tests
- Deployment

---

## 40. Key Architectural Principle

The most important part of the application is **not the UI; it is the table-allocation engine**.

The allocation operation should be treated as an atomic business transaction:

```text
TABLE BECOMES AVAILABLE
          ↓
    Acquire database lock
          ↓
 Find earliest eligible party
          ↓
     No party?
       /     \
     YES      NO
      ↓        ↓
Assign table  Leave available
      ↓
Update waitlist
      ↓
Create notification
      ↓
Release transaction
      ↓
Send SMS asynchronously
```

This design prevents race conditions and gives the application a reliable foundation as the number of restaurants and waitlist transactions grows.

The MVP should therefore focus on getting **multi-tenancy, authorization, table state, waitlist ordering, and atomic assignment** correct before adding more sophisticated restaurant features.
