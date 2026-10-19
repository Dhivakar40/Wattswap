FROM node:22-alpine AS build
RUN corepack enable
WORKDIR /repo
COPY . .
RUN pnpm install --frozen-lockfile --filter @wattswap/web...
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ARG NEXT_PUBLIC_CHAIN_ID=31337
ARG NEXT_PUBLIC_RPC_URL=http://127.0.0.1:8545
ARG NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID=
ENV NEXT_PUBLIC_API_URL=$NEXT_PUBLIC_API_URL     NEXT_PUBLIC_CHAIN_ID=$NEXT_PUBLIC_CHAIN_ID     NEXT_PUBLIC_RPC_URL=$NEXT_PUBLIC_RPC_URL     NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID=$NEXT_PUBLIC_WALLETCONNECT_PROJECT_ID
RUN pnpm --filter @wattswap/web build

FROM node:22-alpine
WORKDIR /app
ENV NODE_ENV=production PORT=3000 HOSTNAME=0.0.0.0
COPY --from=build /repo/apps/web/.next/standalone ./
COPY --from=build /repo/apps/web/.next/static ./apps/web/.next/static
EXPOSE 3000
CMD ["node", "apps/web/server.js"]
