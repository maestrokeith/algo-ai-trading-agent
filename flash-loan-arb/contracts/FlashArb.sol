// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

interface IERC20 {
    function balanceOf(address) external view returns (uint256);
}
interface IPool {
    function flashLoanSimple(address,address,uint256,bytes calldata,uint16) external;
}
interface IRouter {
    function getAmountsOut(uint256,address[] calldata) external view returns (uint256[] memory);
    function swapExactTokensForTokens(uint256,uint256,address[] calldata,address,uint256)
        external returns (uint256[] memory);
}

/// @notice Research receiver for standard ERC20s and two trusted V2 routers.
/// @dev No arbitrary calls. Validate deployments independently before real use.
contract FlashArb {
    struct Trade {
        bool reverse;
        uint256 amount;
        uint256 minOutA;
        uint256 minOutB;
        uint256 minProfit;
        uint256 deadline;
        uint256 maxPremiumBps;
    }
    uint256 public constant MAX_BORROW = 10 ether; // This build borrows 18-decimal WETH only.
    uint256 public constant MAX_SLIPPAGE_BPS = 50;
    address public immutable owner;
    address public immutable pool;
    address public immutable asset;
    address public immutable middle;
    address public immutable routerA;
    address public immutable routerB;
    bool public paused;
    uint256 private phase;
    uint256 private baseline;
    bytes32 private commitment;
    event Completed(uint256 amount, uint256 premium, uint256 profit);

    constructor(address p, address a, address m, address r1, address r2) {
        require(p.code.length > 0 && a.code.length > 0 && m.code.length > 0, "code");
        require(r1.code.length > 0 && r2.code.length > 0 && r1 != r2 && a != m, "route");
        owner = msg.sender; pool = p; asset = a; middle = m; routerA = r1; routerB = r2;
    }
    modifier onlyOwner() { require(msg.sender == owner, "owner"); _; }
    function setPaused(bool value) external onlyOwner {
        require(phase == 0, "busy"); paused = value;
    }
    function execute(Trade calldata t) external onlyOwner returns (uint256 profit) {
        require(phase == 0 && !paused, "inactive");
        require(t.amount > 0 && t.amount <= MAX_BORROW && t.minProfit > 0 && t.minOutA > 0 && t.minOutB > 0, "bounds");
        require(t.deadline >= block.timestamp && t.deadline <= block.timestamp + 120, "deadline");
        require(t.maxPremiumBps <= 100, "fee cap");
        baseline = _balance(asset);
        bytes memory params = abi.encode(t);
        commitment = keccak256(params);
        phase = 1;
        IPool(pool).flashLoanSimple(address(this), asset, t.amount, params, 0);
        require(phase == 2, "callback missing");
        uint256 remaining = _balance(asset);
        require(remaining >= baseline + t.minProfit, "net profit");
        profit = remaining - baseline;
        _approve(asset, pool, 0);
        uint256 ownerBefore = IERC20(asset).balanceOf(owner);
        _call(asset, abi.encodeWithSignature("transfer(address,uint256)", owner, profit));
        require(IERC20(asset).balanceOf(owner) == ownerBefore + profit, "payout");
        commitment = bytes32(0); baseline = 0; phase = 0;
    }
    function executeOperation(address a, uint256 amount, uint256 premium, address initiator, bytes calldata params)
        external returns (bool) {
        require(msg.sender == pool && initiator == address(this) && phase == 1, "callback");
        require(a == asset && keccak256(params) == commitment, "parameters");
        Trade memory t = abi.decode(params, (Trade));
        require(amount == t.amount && block.timestamp <= t.deadline, "stale");
        // Aave uses percentage rounding; allow one smallest unit above floor.
        require(premium <= amount * t.maxPremiumBps / 10000 + 1, "premium");
        require(_balance(asset) >= baseline + amount, "loan missing");
        phase = 2;
        address buy = t.reverse ? routerB : routerA;
        address sell = t.reverse ? routerA : routerB;
        uint256 midBefore = _balance(middle);
        _swap(buy, asset, middle, amount, t.minOutA, t.deadline);
        uint256 acquired = _balance(middle) - midBefore;
        require(acquired >= t.minOutA, "first output");
        _swap(sell, middle, asset, acquired, t.minOutB, t.deadline);
        uint256 owed = amount + premium;
        uint256 ending = _balance(asset);
        require(ending >= baseline + owed + t.minProfit, "unprofitable");
        _approve(asset, pool, owed);
        emit Completed(amount, premium, ending - baseline - owed);
        return true;
    }
    function rescue(address token, uint256 amount) external onlyOwner {
        require(phase == 0, "busy");
        _call(token, abi.encodeWithSignature("transfer(address,uint256)", owner, amount));
    }
    function _swap(address router, address input, address output, uint256 amount, uint256 minimum, uint256 deadline) private {
        _approve(input, router, 0); _approve(input, router, amount);
        address[] memory path = new address[](2); path[0] = input; path[1] = output;
        uint256[] memory quote = IRouter(router).getAmountsOut(amount, path);
        require(quote.length == 2 && quote[1] > 0, "quote");
        uint256 floor = quote[1] * (10000 - MAX_SLIPPAGE_BPS) / 10000;
        if (minimum < floor) minimum = floor;
        IRouter(router).swapExactTokensForTokens(amount, minimum, path, address(this), deadline);
        _approve(input, router, 0);
    }
    function _balance(address token) private view returns (uint256 value) {
        assembly ("memory-safe") {
            let ptr := mload(0x40)
            mstore(ptr, shl(224, 0x70a08231))
            mstore(add(ptr, 4), address())
            if iszero(staticcall(gas(), token, ptr, 36, ptr, 32)) { revert(0, 0) }
            if iszero(eq(returndatasize(), 32)) { revert(0, 0) }
            value := mload(ptr)
        }
    }
    function _approve(address token, address spender, uint256 amount) private {
        _call(token, abi.encodeWithSignature("approve(address,uint256)", spender, amount));
    }
    function _call(address token, bytes memory data) private {
        require(token.code.length > 0, "token code");
        (bool ok, bytes memory result) = token.call(data);
        require(ok && (result.length == 0 || abi.decode(result, (bool))), "token operation");
    }
}
