package demo.user;
public class UserService { private UserRepository repo; public User find(String e){ return repo.findByEmail(e);} }
