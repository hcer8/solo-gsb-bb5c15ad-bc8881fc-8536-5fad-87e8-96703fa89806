"""Shared SDL schema for the test-suite."""

SCHEMA = """
directive @cost(complexity: Int, multiplier: Int) on FIELD_DEFINITION

type Query {
  user(id: ID!): User
  search(text: String): [SearchResult]
  node(id: ID!): Node
}

interface Node {
  id: ID!
}

union SearchResult = User | Repository

type User implements Node {
  id: ID!
  name: String
  email: String @cost(complexity: 3)
  friends(first: Int): [User!]!
  followers: [User] @cost(multiplier: 50)
  repositories(first: Int): [Repository!]!
}

type Repository implements Node {
  id: ID!
  name: String
  tags: [String]
  owner: User
}
"""

SIMPLE_QUERY = """
{ user(id: "1") { id name } }
"""

NESTED_LIST_QUERY = """
{ user(id: "1") { friends { repositories { tags } } } }
"""

CYCLE_QUERY = """
fragment A on User { friends { ...B } }
fragment B on User { friends { ...A } }
query { user(id: "1") { ...A } }
"""
