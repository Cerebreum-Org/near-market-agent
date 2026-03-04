# NEAR Protocol Reference — Agent Knowledge Base

## Overview
NEAR is a sharded, proof-of-stake Layer 1 blockchain. Key differentiators: human-readable account names, sharding (Nightshade), low fees, ~1s finality.

## Networks
| Network | RPC Endpoint | Explorer |
|---------|-------------|----------|
| Mainnet | `https://rpc.mainnet.near.org` | `https://nearblocks.io` |
| Testnet | `https://rpc.testnet.near.org` | `https://testnet.nearblocks.io` |

## Account Model
- **Named accounts**: `alice.near`, `app.alice.near` (subaccounts)
- **Implicit accounts**: 64-char hex derived from public key
- Accounts hold NEAR balance + can store contract code
- Storage staking: ~1 NEAR per 100KB of on-chain storage

## Gas & Fees
- Gas unit: **TGas** (1 TGas = 10^12 gas units)
- Typical function call: 5-30 TGas
- Max gas per transaction: 300 TGas
- Cost: ~0.0001 NEAR per TGas (very cheap)
- 30% of gas fees burned, 70% to contract

## SDKs

### near-api-js (JavaScript/TypeScript)
```typescript
import { connect, keyStores, utils } from "near-api-js";

const near = await connect({
  networkId: "mainnet",
  keyStore: new keyStores.InMemoryKeyStore(),
  nodeUrl: "https://rpc.mainnet.near.org",
});

const account = await near.account("alice.near");

// View call (free, read-only)
const result = await account.viewFunction({
  contractId: "contract.near",
  methodName: "get_status",
  args: { account_id: "bob.near" },
});

// Change call (costs gas)
await account.functionCall({
  contractId: "contract.near",
  methodName: "set_status",
  args: { message: "hello" },
  gas: "30000000000000",  // 30 TGas
  attachedDeposit: utils.format.parseNearAmount("0.1"),
});
```

### near-sdk-rs (Rust Smart Contracts)
```rust
use near_sdk::borsh::{BorshDeserialize, BorshSerialize};
use near_sdk::{near, env, NearToken, AccountId};

#[near(contract_state)]
pub struct Contract {
    owner: AccountId,
    records: near_sdk::store::LookupMap<AccountId, String>,
}

#[near]
impl Contract {
    #[init]
    pub fn new(owner: AccountId) -> Self {
        Self { owner, records: near_sdk::store::LookupMap::new(b"r") }
    }

    pub fn get_record(&self, account_id: AccountId) -> Option<&String> {
        self.records.get(&account_id)
    }

    #[payable]
    pub fn set_record(&mut self, value: String) {
        let sender = env::predecessor_account_id();
        self.records.insert(sender, value);
    }
}
```

### near-sdk-js (JavaScript Smart Contracts)
```javascript
import { NearBindgen, call, view, near } from "near-sdk-js";

@NearBindgen({})
class Contract {
  records = {};

  @view({})
  get_record({ account_id }) {
    return this.records[account_id] || null;
  }

  @call({})
  set_record({ value }) {
    const sender = near.predecessorAccountId();
    this.records[sender] = value;
  }
}
```

## Common Contract Standards
- **NEP-141**: Fungible Token (FT) — `ft_transfer`, `ft_balance_of`, `ft_total_supply`
- **NEP-171**: Non-Fungible Token (NFT) — `nft_transfer`, `nft_token`, `nft_tokens_for_owner`
- **NEP-145**: Storage Management — `storage_deposit`, `storage_withdraw`, `storage_balance_of`
- **NEP-148**: FT Metadata — `ft_metadata()` returns name, symbol, decimals, icon
- **NEP-177**: NFT Metadata — `nft_metadata()` returns name, symbol, base_uri

## NEAR CLI
```bash
# Install
npm install -g near-cli-rs

# Login
near login

# View account
near view-account alice.near

# Call view function
near view contract.near get_status '{"account_id": "bob.near"}'

# Call change function
near call contract.near set_status '{"message": "hello"}' --accountId alice.near --deposit 0.1

# Deploy contract
near deploy contract.near ./target/wasm32-unknown-unknown/release/contract.wasm

# Create subaccount
near create-account sub.alice.near --masterAccount alice.near --initialBalance 5
```

## Key DeFi Protocols on NEAR
- **Ref Finance**: DEX (AMM), contract `v2.ref-finance.near`
- **Burrow**: Lending/borrowing, contract `contract.main.burrow.near`
- **Meta Pool**: Liquid staking (stNEAR), contract `meta-pool.near`
- **Orderly Network**: Orderbook DEX infra

## MCP Server Patterns for NEAR
```typescript
import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";

const server = new McpServer({ name: "near-tools", version: "1.0.0" });

server.tool("get_account_balance", "Get NEAR balance for an account", {
  account_id: { type: "string", description: "NEAR account ID" },
}, async ({ account_id }) => {
  const response = await fetch("https://rpc.mainnet.near.org", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      jsonrpc: "2.0", id: "1", method: "query",
      params: { request_type: "view_account", finality: "final", account_id },
    }),
  });
  const data = await response.json();
  const balance = data.result?.amount;
  const nearBalance = balance ? (BigInt(balance) / BigInt(10**24)).toString() : "0";
  return { content: [{ type: "text", text: `${account_id}: ${nearBalance} NEAR` }] };
});

const transport = new StdioServerTransport();
await server.connect(transport);
```

## Useful Links
- Docs: https://docs.near.org
- Examples: https://github.com/near-examples
- near-api-js: https://github.com/near/near-api-js
- NEAR Enhancement Proposals: https://github.com/near/NEPs
