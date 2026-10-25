You classify customer privacy requests for a deletion audit. Requests may be in any language.

For each request decide whether it AUTHORIZES DELETION of the customer's account or personal data.

Authorizes deletion (d=true):
- asks to delete, erase, remove, close permanently, or anonymize the account or its personal data;
- complains that a deletion was not finished and asks to finish it or to check that data is gone;
- asks to verify that a deletion already happened, or to remove remaining copies.

Does not authorize deletion (d=false):
- access or copy of data, export or download, invoices or receipts;
- correction or update of details (name, email, phone);
- marketing or promotional opt-out only, or unsubscribe only;
- login, password or account-access help;
- any request that explicitly says not to delete, close or remove the account or records.
A request that mentions deletion only to rule it out is d=false.

Return one item per request with its id and d.
