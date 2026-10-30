// SPDX-License-Identifier: MIT
pragma solidity 0.8.34;

contract Vault {
    address public owner;

    constructor() {
        owner = msg.sender;
    }

    function deposit() external payable {}

    function setOwner(address newOwner) external {
        owner = newOwner;
    }

    function sweep(address payable to) external {
        require(msg.sender == owner, "not owner");
        to.transfer(address(this).balance);
    }
}