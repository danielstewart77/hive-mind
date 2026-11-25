# Microsoft and Windows domain sign-in

Both are off until configured, and each switches on by configuration alone:
with its settings present the sign-in page offers "Sign in with Microsoft" or
"Sign in with Windows"; without them the link is absent and its route answers
404. Either signs in only to an account an admin has already created under
Settings, Users and groups. Anyone else is told their account has not been
set up; a disabled account is refused. Either one yields a console session
and nothing else: never an auth-service `auth_session` another sparktobloom
site would accept.

An account made only for these methods is created with no temporary password,
so it has no password to sign in with and nothing to change.

## Microsoft (Entra ID)

OpenID Connect, authorization code with PKCE, against one tenant. A token is
accepted only when its signature verifies against the tenant's published
keys and its issuer, tenant id (`tid`), audience, nonce and expiry are this
app's.

The first sign-in selects the account whose email equals the token's `email`
claim, ignoring case, and binds that account to the token's object id
(`oid`). Every later sign-in matches on the object id alone, so an email
the tenant later reports differently does not lock the owner out, and a
different person who later holds that email address in the tenant is refused
rather than handed the account. The binding is kept in the console's own store
(`core.sso_bindings` in the hive's Postgres) on both account stores, because auth-service has
no field for it.

An admin can unbind an account (Settings, Users and groups, "Unbind
Microsoft"; `DELETE /api/admin/users/<username>/microsoft`), and changing an
account's email unbinds it, since the binding was made on the old address.
Either way the account's next Microsoft sign-in selects it by email again. A token that says its email is unverified (`email_verified`
or `xms_edov` false) matches no account by email.

Works with either account store (the built-in accounts, or auth-service at
home). At home
an account signs in only if auth-service grants it the `hive` site, and an
admin counts as granted, as with `/verify`.

### What IT provides

An app registration in the organization's Entra tenant:

- **Supported account types:** accounts in this organizational directory
  only (single tenant).
- **Platform:** Web, with the redirect URI
  `https://<console host>/login/microsoft/callback` — exactly the value of
  `HIVE_MICROSOFT_REDIRECT_URL`.
- **A client secret** (Certificates & secrets), and a reminder of its expiry.
- **Token configuration:** add the optional claim `email` to the ID token
  (and `xms_edov` if available), so the first sign-in has an email to match.
- **API permissions:** Microsoft Graph delegated `openid`, `email`, `profile`
  (the defaults; no admin-consented application permissions are needed).
- From the Overview page: the **Directory (tenant) ID** and the
  **Application (client) ID**.

### Configuration

| Variable | Value |
| --- | --- |
| `HIVE_MICROSOFT_TENANT_ID` | Directory (tenant) ID, a GUID. `common` and `organizations` are refused: one tenant is pinned. |
| `HIVE_MICROSOFT_CLIENT_ID` | Application (client) ID |
| `HIVE_MICROSOFT_CLIENT_SECRET` | The client secret's value |
| `HIVE_MICROSOFT_REDIRECT_URL` | `https://<console host>/login/microsoft/callback` |
| `HIVE_MICROSOFT_AUTHORITY` | Optional. Default `https://login.microsoftonline.com`; set for a national cloud. |

All four required values must be present for the method to appear.

## Windows domain (Kerberos)

SPNEGO with Kerberos, offered only behind the "Sign in with Windows" link.
The plain sign-in page never asks the browser for a ticket. Following the
link, the console asks for one; a browser that has a ticket for the console's
service principal sends it and is signed in, and a browser that has none, or
offers NTLM, or offers a ticket that does not verify, lands back on the
sign-in page with the other methods. Only a valid ticket for an account
nobody created shows "not set up".

The ticket's client principal `user@REALM` names the account whose domain
account is `DOMAIN\user`, ignoring case. The domain for a realm is its entry
in `HIVE_KERBEROS_REALM_MAP`; with no map configured, the realm's first label
(`CORP.EXAMPLE.EDU` gives `CORP`). Once a map is configured, a realm it does
not name signs nobody in, so a trusted foreign realm cannot reach an account
by accident. A principal with an instance (`user/admin@REALM`) never signs
in.

Domain accounts are held only by the console's own account store, so this
method is available only on an install that signs in against the built-in
accounts; at
home, behind auth-service, it stays off whatever is configured.

### What IT provides

- **The console's host joined to the domain**, or at least a computer or
  service account in the domain to carry its service principal.
- **A service principal** `HTTP/<console host>` (the fully qualified name
  people type in the browser), registered on that account — for example
  `setspn -S HTTP/hive.corp.example.edu HIVESVC`.
- **A keytab** holding that principal's key, for example
  `ktpass /princ HTTP/hive.corp.example.edu@CORP.EXAMPLE.EDU /mapuser CORP\HIVESVC /crypto AES256-SHA1 /ptype KRB5_NT_PRINCIPAL /pass * /out hive.keytab`
  on Windows, or `samba-tool domain exportkeytab hive.keytab --principal=HTTP/hive.corp.example.edu`
  against a Samba DC. Mount it into the container read-only and readable by
  the console only.
- **Browser policy** placing the console's host in the Local intranet zone
  (Edge, Chrome and IE on Windows), or on the
  `AuthServerAllowlist` / `network.negotiate-auth.trusted-uris` list
  elsewhere. Without it the browser never sends a ticket and the user gets
  the other methods.
- **Reach the console by that host name.** The browser asks for a ticket for
  `HTTP/<the name in the address bar>`; an IP address, a short name or a
  different alias asks for a principal that does not exist.
- The admin then creates each person's account with their domain account
  filled in (`CORP\sloan`).

### Configuration

| Variable | Value |
| --- | --- |
| `HIVE_KERBEROS_KEYTAB` | Path to the keytab inside the container. The method is off until the file exists. |
| `HIVE_KERBEROS_SPN` | The service principal, with its realm: `HTTP/hive.corp.example.edu@CORP.EXAMPLE.EDU`. Without a realm, `/etc/krb5.conf`'s `default_realm` supplies it, and the image ships no krb5.conf. |
| `HIVE_KERBEROS_REALM_MAP` | Optional. `REALM=DOMAIN` pairs, comma-separated: `CORP.EXAMPLE.EDU=CORP,LAB.EXAMPLE.EDU=LAB`. |

The image compiles `python-gssapi` against MIT Kerberos
(`requirements-kerberos.txt`); a development venv does not need it.
