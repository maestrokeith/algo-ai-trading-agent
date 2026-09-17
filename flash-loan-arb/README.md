# Flash Arb Lab

A free-to-run, dependency-free Node.js research bot with an Aave V3 flash-loan receiver, a local dashboard, and an Anvil fork runner. No subscription, exchange account, wallet connection, or private key is required for demo or read-only scanning.

**Default mode never sends transactions. Real execution is not deployed or enabled.** Estimated opportunities are not earnings. The receiver and execution adapter are research software, not audited production infrastructure.

## Start on Termux

```bash
pkg install nodejs-lts git
git clone --branch codex/flash-loan-arb --single-branch https://github.com/maestrokeith/algo-ai-trading-agent.git flash-arb-workspace
cd flash-arb-workspace/flash-loan-arb
npm test
npm start
```

Open http://127.0.0.1:8787 on the same device. Node.js 22+ is required. There is no `npm install`: all runtime and JavaScript test dependencies are built in. The initial dashboard clearly labels its invented reserves as DEMO.

## Read real chain prices for free

Stop the dashboard with Ctrl+C, then:

```bash
export RPC_URL=https://ethereum-rpc.publicnode.com
npm start
```

Or run one scan / continuous CLI logging:

```bash
npm run scan
npm run watch
```

PublicNode publishes the endpoint at https://ethereum.publicnode.com/. Free RPC has availability and rate limits; no latency guarantee is implied. HTTP RPC errors fail closed, with no synthetic fallback. CLI logs rotate at approximately 5 MB. The dashboard listens only on localhost and is not a 24/7 hosted service. Termux may stop when Android suspends it.

For a provider that supports Ethereum WebSocket subscriptions, set `WS_RPC_URL` and restart. The dashboard subscribes to pending hashes and new heads, coalesces signals, and rescans at most once per five seconds. It also polls every 15 seconds. This is public-pending notification monitoring, not access to private order flow and not replay of pending transactions. Flashbots Protect is not assumed to expose a public mempool feed. Custom RPC/gRPC infrastructure may cost money; no paid service was provisioned.

## What is implemented

| Layer | Implementation |
|---|---|
| Market | Ethereum WETH → USDC → WETH; Uniswap V2 and Sushi V2, both directions |
| Graph | Bellman-Ford negative-cycle screening with parallel exchange edges; reusable detector also supports larger graphs |
| Quotes | Amount-specific router quotes include the deployed router's swap fee and pool price impact; BigInt accounting, not floating-point money |
| Sizing | Six trial amounts from 0.1 through 10 WETH; selects economically eligible candidates, not a claim of globally optimal sizing |
| Consistency | Quote calls pinned to one block; age, chain, deployed-code, token decimals, router-WETH, shared-pool and reorg checks |
| Simulation | Full local Anvil transaction, `eth_call`, `eth_estimateGas`, local receipt, and balance-delta reporting |
| Contract | Solidity 0.8.24; Aave callback authentication and request commitment; phase reentrancy guard; exact approvals reset to zero; owner-only execution and pause |
| Bounds | 10 WETH maximum borrow, ≤0.5% per-swap on-chain quote tolerance, 0.3% scanner buffer, short deadlines, premium cap and minimum profit |
| Yul | Memory-safe ERC20 balance `staticcall`; swap calls use typed trusted-router interfaces |
| Breaker | Three scan failures trigger a 60-second cooldown; on-chain emergency pause |
| Fee policy | EIP-1559 next-base-fee bound, observed-priority input, missed-inclusion adjustment, tip and retained-profit caps |
| Private adapter | Signed EIP-1559 payload validation; Flashbots simulation then private bundle submission; external auth signer; disabled by default; no public broadcast fallback |

The graph engine is general, but this receiver intentionally executes only the two configured WETH/USDC legs. It does not execute triangular routes, V3/V4 pools, cross-chain arbitrage, or arbitrary calldata. A graph signal alone never approves a trade. The Aave pool is resolved from its Ethereum address provider and its premium is read on-chain rather than hardcoded.

## Fork execution (Linux/macOS development machine)

Install the free Foundry tools following https://getfoundry.sh/introduction/installation/ . Foundry is only needed for Solidity tests and fork simulation; it is not required for the Termux dashboard.

Terminal 1:

```bash
export RPC_URL=https://ethereum-rpc.publicnode.com
anvil --fork-url "$RPC_URL" --chain-id 31337 --host 127.0.0.1
```

Terminal 2, promptly after starting the fork:

```bash
cd flash-arb-workspace/flash-loan-arb
forge test -vv
npm run fork
```

The runner accepts only a localhost RPC reporting chain ID 31337. It compiles the receiver, reads the fork, and, if there is an eligible candidate, deploys and executes using Anvil's unlocked fake account. There is no mainnet deployment command. If no profitable route exists, it reports that fact and sends no trade. Restart an old fork: stale snapshots are rejected. Free endpoints may not provide the historical state required by Anvil.

The local execution report subtracts actual fork gas from the WETH payout. Deployment cost is separate and excluded from per-trade net. Fork balances and profits are not real. Local simulation cannot guarantee next-block inclusion, identical live state, or profitable execution.

## Private execution integration boundary

`src/private-relay.mjs` exports `PrivateRelay`. Nothing in the CLI, dashboard, or fork runner imports it. `enableSubmission` defaults to false. It requires:

1. An independently reviewed receiver already deployed with the intended external signer as owner.
2. A signed EIP-1559 transaction produced outside this process, targeting only that receiver's `execute` function.
3. An `authSigner(digestHex)` callback returning `address:signature`, signing the UTF-8 digest string using EIP-191, as required by Flashbots. Relay authentication can use a separate identity from the funds signer.
4. An explicit integration that enables private submission after its own validation.

The adapter verifies chain, target, zero ETH value, borrow bounds, transaction gas/fee ceilings and an on-chain profit floor covering the entire gas-limit × fee-cap budget plus retained profit. It calls `eth_callBundle`, rejects errors and insufficient return value, checks that the head has not changed, and only then calls `eth_sendBundle` for the next block. It does not rebroadcast publicly on failure, retry stale signed transactions, or log credentials. You must externally manage nonce, replacement, inclusion receipts and signing policy. Private routing reduces public exposure; builders and relay operators still receive the transaction and inclusion is not guaranteed.

`feeBudget()` in `src/governance.mjs` is a bounded planning function. Feed it fresh fee-market observations and actual inclusion outcomes. It does not pretend to observe competitors' private bids and is not wired to an autonomous funds signer.

## Tests and limits

```bash
npm test          # Node math, ABI, scanner, graph, breaker and relay mocks
forge test -vv    # Atomic repayment, ownership, callbacks, cap, pause and loss reversal
```

GitHub Actions runs both suites. Relay tests use a fake server; they do not send bundles. Solidity tests use mock pools/routers to test invariants and do not prove integration with the current Aave deployment. Real RPC and fork checks must succeed before considering deployment. This build has no claim of profitability or production security audit.

## Source references

- Aave flash loans: https://aave.com/docs/aave-v3/guides/flash-loans
- Aave address book: https://github.com/bgd-labs/aave-address-book
- Uniswap V2 Router02: https://github.com/Uniswap/v2-periphery/blob/master/contracts/UniswapV2Router02.sol
- Flashbots bundle API: https://docs.flashbots.net/flashbots-auction/advanced/rpc-endpoint
- Flashbots EIP-1559: https://docs.flashbots.net/flashbots-auction/advanced/eip1559
- Geth subscriptions: https://geth.ethereum.org/docs/interacting-with-geth/rpc/pubsub

No flash-loan collateral is deposited. Mainnet contract deployment, successful execution, and included reverted transactions can still incur gas costs. No paid RPC, subscription, live deployment or real transaction was authorized or purchased by this build.
