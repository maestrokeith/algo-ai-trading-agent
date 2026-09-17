// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;
import "../contracts/FlashArb.sol";
interface Vm { function prank(address) external; function expectRevert() external; function warp(uint256) external; }
contract MockToken {
    mapping(address=>uint256) public balanceOf;
    mapping(address=>mapping(address=>uint256)) public allowance;
    function mint(address to,uint256 n) external {balanceOf[to]+=n;}
    function approve(address to,uint256 n) external returns(bool){allowance[msg.sender][to]=n;return true;}
    function transfer(address to,uint256 n) external returns(bool){_move(msg.sender,to,n);return true;}
    function transferFrom(address from,address to,uint256 n) external returns(bool){
        require(allowance[from][msg.sender]>=n,"allowance");allowance[from][msg.sender]-=n;_move(from,to,n);return true;
    }
    function _move(address from,address to,uint256 n) private {require(balanceOf[from]>=n,"balance");balanceOf[from]-=n;balanceOf[to]+=n;}
}
contract MockPool {
    uint256 public fee=5;
    bool public wrongInitiator;
    function setFee(uint256 n) external {fee=n;}
    function setWrongInitiator(bool b) external {wrongInitiator=b;}
    function flashLoanSimple(address receiver,address asset,uint256 n,bytes calldata params,uint16) external {
        uint256 premium=(n*fee+5000)/10000;
        MockToken(asset).transfer(receiver,n);
        require(FlashArb(receiver).executeOperation(asset,n,premium,wrongInitiator?address(1):msg.sender,params));
        MockToken(asset).transferFrom(receiver,address(this),n+premium);
    }
}
contract MockRouter {
    uint256 public rate=10000;
    function getAmountsOut(uint256 n,address[] calldata) external view returns(uint256[] memory a){a=new uint256[](2);a[0]=n;a[1]=n*rate/10000;}
    function setRate(uint256 n) external {rate=n;}
    function swapExactTokensForTokens(uint256 n,uint256 minimum,address[] calldata path,address to,uint256 deadline)
        external returns(uint256[] memory amounts){
        require(block.timestamp<=deadline,"deadline");
        uint256 output=n*rate/10000;require(output>=minimum,"slippage");
        MockToken(path[0]).transferFrom(msg.sender,address(this),n);
        MockToken(path[1]).transfer(to,output);
        amounts=new uint256[](2);amounts[0]=n;amounts[1]=output;
    }
}
contract FlashArbTest {
    Vm constant vm=Vm(address(uint160(uint256(keccak256("hevm cheat code")))));
    MockToken a;MockToken b;MockPool pool;MockRouter r1;MockRouter r2;FlashArb arb;
    function setUp() public {
        vm.warp(1000);
        a=new MockToken();b=new MockToken();pool=new MockPool();r1=new MockRouter();r2=new MockRouter();
        r2.setRate(10200);
        arb=new FlashArb(address(pool),address(a),address(b),address(r1),address(r2));
        a.mint(address(pool),1000 ether);b.mint(address(r1),1000 ether);a.mint(address(r2),1000 ether);
    }
    function trade() private view returns(FlashArb.Trade memory){
        return FlashArb.Trade(false,1 ether,1 ether,1 ether,0.001 ether,block.timestamp+60,5);
    }
    function testProfitRepaidAndTransferred() public {
        uint256 p=arb.execute(trade());
        require(p==0.0195 ether,"profit");require(a.balanceOf(address(this))==p,"payout");
        require(a.balanceOf(address(pool))==1000.0005 ether,"repayment");
        require(a.balanceOf(address(arb))==0,"residual");
        require(a.allowance(address(arb),address(pool))==0,"pool allowance");
        require(a.allowance(address(arb),address(r1))==0,"router allowance");
        require(b.allowance(address(arb),address(r2))==0,"mid allowance");
    }
    function testDonationsCannotSubsidizeLoss() public {
        a.mint(address(arb),1 ether);r2.setRate(9999);
        FlashArb.Trade memory t=trade();t.minOutB=0.99 ether;
        vm.expectRevert();arb.execute(t);
        require(a.balanceOf(address(arb))==1 ether,"donation spent");
        require(a.balanceOf(address(pool))==1000 ether,"loan not rolled back");
    }
    function testExistingMiddleBalanceNotSpent() public {
        b.mint(address(arb),3 ether);arb.execute(trade());require(b.balanceOf(address(arb))==3 ether,"middle spent");
    }
    function testOnlyOwner() public {FlashArb.Trade memory t=trade();vm.prank(address(123));vm.expectRevert();arb.execute(t);}
    function testPaused() public {arb.setPaused(true);FlashArb.Trade memory t=trade();vm.expectRevert();arb.execute(t);}
    function testExpired() public {FlashArb.Trade memory t=trade();t.deadline=999;vm.expectRevert();arb.execute(t);}
    function testExcessiveDeadline() public {FlashArb.Trade memory t=trade();t.deadline=9999;vm.expectRevert();arb.execute(t);}
    function testExcessivePremium() public {pool.setFee(100);FlashArb.Trade memory t=trade();vm.expectRevert();arb.execute(t);}
    function testWrongInitiator() public {pool.setWrongInitiator(true);FlashArb.Trade memory t=trade();vm.expectRevert();arb.execute(t);}
    function testFakeCallback() public {vm.expectRevert();arb.executeOperation(address(a),1 ether,0,address(arb),"");}
    function testUnsolicitedLoan() public {
        FlashArb.Trade memory t=trade();vm.expectRevert();
        pool.flashLoanSimple(address(arb),address(a),1 ether,abi.encode(t),0);
    }
    function testSlippageRevertsWholeLoan() public {
        FlashArb.Trade memory t=trade();t.minOutA=2 ether;vm.expectRevert();arb.execute(t);
        require(a.balanceOf(address(pool))==1000 ether,"not atomic");
    }
    function testBorrowCap() public {FlashArb.Trade memory t=trade();t.amount=11 ether;vm.expectRevert();arb.execute(t);}
    function testZeroProfitGuardRejected() public {
        FlashArb.Trade memory t=trade();t.minProfit=0;vm.expectRevert();arb.execute(t);
    }
    function testSecondExecutionWorks() public {arb.execute(trade());arb.execute(trade());require(a.balanceOf(address(this))==0.039 ether);}
    function testRescueOwnerOnly() public {
        a.mint(address(arb),1 ether);vm.prank(address(123));vm.expectRevert();arb.rescue(address(a),1 ether);
        arb.rescue(address(a),1 ether);require(a.balanceOf(address(this))==1 ether);
    }
}
