import {parseUnits} from './abi.mjs';
export const config = Object.freeze({
  chainId: 1n,
  provider: '0x2f39d218133afab8f2b819b1066c7e434ad94e9e',
  asset: '0xc02aaa39b223fe8d0a0e5c4f27ead9083c756cc2',
  middle: '0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48',
  routers: [
    {name:'Uniswap V2',address:'0x7a250d5630b4cf539739df2c5dacb4c659f2488d'},
    {name:'Sushi V2',address:'0xd9e1ce17f2641f24ae83637ab66a2cca9c378b9f'}
  ],
  amounts: ['0.1','0.5','1','2','5','10'].map(x=>parseUnits(x)),
  slippageBps: 30n,
  minNetProfit: parseUnits('0.0005'),
  gasUnits: 650000n,
  maxBlockAgeSeconds: 90,
  pollMs: 15000
});
